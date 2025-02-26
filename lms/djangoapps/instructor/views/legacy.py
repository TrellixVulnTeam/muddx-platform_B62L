"""
Instructor Views
"""
## NOTE: This is the code for the legacy instructor dashboard
## We are no longer supporting this file or accepting changes into it.

from contextlib import contextmanager
import csv
import json
import logging
import os
import re
import requests

from collections import defaultdict, OrderedDict
from markupsafe import escape
from requests.status_codes import codes
from StringIO import StringIO

from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpResponse
from django_future.csrf import ensure_csrf_cookie
from django.views.decorators.cache import cache_control
from django.core.urlresolvers import reverse
from django.core.mail import send_mail
from django.utils import timezone

from xmodule_modifiers import wrap_xblock
import xmodule.graders as xmgraders
from xmodule.modulestore import XML_MODULESTORE_TYPE
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.locations import SlashSeparatedCourseKey
from xmodule.modulestore.exceptions import ItemNotFoundError
from xmodule.html_module import HtmlDescriptor
from opaque_keys import InvalidKeyError
from lms.lib.xblock.runtime import quote_slashes

# Submissions is a Django app that is currently installed
# from the edx-ora2 repo, although it will likely move in the future.
from submissions import api as sub_api

from bulk_email.models import CourseEmail, CourseAuthorization
from courseware import grades
from courseware.access import has_access
from courseware.courses import get_course_with_access, get_cms_course_link
from student.roles import (
    CourseStaffRole, CourseInstructorRole, CourseBetaTesterRole, GlobalStaff
)
from courseware.models import StudentModule
from django_comment_common.models import (
    Role, FORUM_ROLE_ADMINISTRATOR, FORUM_ROLE_MODERATOR, FORUM_ROLE_COMMUNITY_TA
)
from django_comment_client.utils import has_forum_access
from instructor.offline_gradecalc import student_grades, offline_grades_available
from instructor.views.tools import strip_if_string, bulk_email_is_enabled_for_course
from instructor_task.api import (
    get_running_instructor_tasks,
    get_instructor_task_history,
    submit_rescore_problem_for_all_students,
    submit_rescore_problem_for_student,
    submit_reset_problem_attempts_for_all_students,
    submit_bulk_course_email
)
from instructor_task.views import get_task_completion_info
from edxmako.shortcuts import render_to_response, render_to_string
from class_dashboard import dashboard_data
from psychometrics import psychoanalyze
from student.models import (
    CourseEnrollment,
    CourseEnrollmentAllowed,
    unique_id_for_user,
    anonymous_id_for_user
)
from student.views import course_from_id
import track.views
from xblock.field_data import DictFieldData
from xblock.fields import ScopeIds
from django.utils.translation import ugettext as _

from microsite_configuration import microsite
from xmodule.modulestore.locations import i4xEncoder

log = logging.getLogger(__name__)

# internal commands for managing forum roles:
FORUM_ROLE_ADD = 'add'
FORUM_ROLE_REMOVE = 'remove'

# For determining if a shibboleth course
SHIBBOLETH_DOMAIN_PREFIX = 'shib:'


def split_by_comma_and_whitespace(a_str):
    """
    Return string a_str, split by , or whitespace
    """
    return re.split(r'[\s,]', a_str)


@ensure_csrf_cookie
@cache_control(no_cache=True, no_store=True, must_revalidate=True)
def instructor_dashboard(request, course_id):
    """Display the instructor dashboard for a course."""
    course_key = SlashSeparatedCourseKey.from_deprecated_string(course_id)
    course = get_course_with_access(request.user, 'staff', course_key, depth=None)

    instructor_access = has_access(request.user, 'instructor', course)   # an instructor can manage staff lists

    forum_admin_access = has_forum_access(request.user, course_key, FORUM_ROLE_ADMINISTRATOR)

    msg = ''
    email_msg = ''
    email_to_option = None
    email_subject = None
    html_message = ''
    show_email_tab = False
    problems = []
    plots = []
    datatable = {}

    # the instructor dashboard page is modal: grades, psychometrics, admin
    # keep that state in request.session (defaults to grades mode)
    idash_mode = request.POST.get('idash_mode', '')
    idash_mode_key = u'idash_mode:{0}'.format(course_id)
    if idash_mode:
        request.session[idash_mode_key] = idash_mode
    else:
        idash_mode = request.session.get(idash_mode_key, 'Grades')

    enrollment_number = CourseEnrollment.num_enrolled_in(course_key)

    # assemble some course statistics for output to instructor
    def get_course_stats_table():
        datatable = {
            'header': ['Statistic', 'Value'],
            'title': _('Course Statistics At A Glance'),
        }
        data = [['# Enrolled', enrollment_number]]
        data += [['Date', timezone.now().isoformat()]]
        data += compute_course_stats(course).items()
        if request.user.is_staff:
            for field in course.fields.values():
                if getattr(field.scope, 'user', False):
                    continue

                data.append([
                    field.name,
                    json.dumps(field.read_json(course), cls=i4xEncoder)
                ])
        datatable['data'] = data
        return datatable

    def return_csv(func, datatable, file_pointer=None):
        """Outputs a CSV file from the contents of a datatable."""
        if file_pointer is None:
            response = HttpResponse(mimetype='text/csv')
            response['Content-Disposition'] = 'attachment; filename={0}'.format(func)
        else:
            response = file_pointer
        writer = csv.writer(response, dialect='excel', quotechar='"', quoting=csv.QUOTE_ALL)
        encoded_row = [unicode(s).encode('utf-8') for s in datatable['header']]
        writer.writerow(encoded_row)
        for datarow in datatable['data']:
            # 's' here may be an integer, float (eg score) or string (eg student name)
            encoded_row = [
                # If s is already a UTF-8 string, trying to make a unicode
                # object out of it will fail unless we pass in an encoding to
                # the constructor. But we can't do that across the board,
                # because s is often a numeric type. So just do this.
                s if isinstance(s, str) else unicode(s).encode('utf-8')
                for s in datarow
            ]
            writer.writerow(encoded_row)
        return response

    def get_student_from_identifier(unique_student_identifier):
        """Gets a student object using either an email address or username"""
        unique_student_identifier = strip_if_string(unique_student_identifier)
        msg = ""
        try:
            if "@" in unique_student_identifier:
                student = User.objects.get(email=unique_student_identifier)
            else:
                student = User.objects.get(username=unique_student_identifier)
            msg += _("Found a single student.  ")
        except User.DoesNotExist:
            student = None
            msg += "<font color='red'>{text}</font>".format(
                text=_("Couldn't find student with that email or username.")
            )
        return msg, student

    # process actions from form POST
    action = request.POST.get('action', '')
    use_offline = request.POST.get('use_offline_grades', False)

    if settings.FEATURES['ENABLE_MANUAL_GIT_RELOAD']:
        if 'GIT pull' in action:
            data_dir = course.data_dir
            log.debug('git pull {0}'.format(data_dir))
            gdir = settings.DATA_DIR / data_dir
            if not os.path.exists(gdir):
                msg += "====> ERROR in gitreload - no such directory {0}".format(gdir)
            else:
                cmd = "cd {0}; git reset --hard HEAD; git clean -f -d; git pull origin; chmod g+w course.xml".format(gdir)
                msg += "git pull on {0}:<p>".format(data_dir)
                msg += "<pre>{0}</pre></p>".format(escape(os.popen(cmd).read()))
                track.views.server_track(request, "git-pull", {"directory": data_dir}, page="idashboard")

        if 'Reload course' in action:
            log.debug('reloading {0} ({1})'.format(course_key, course))
            try:
                data_dir = course.data_dir
                modulestore().try_load_course(data_dir)
                msg += "<br/><p>Course reloaded from {0}</p>".format(data_dir)
                track.views.server_track(request, "reload", {"directory": data_dir}, page="idashboard")
                course_errors = modulestore().get_course_errors(course.id)
                msg += '<ul>'
                for cmsg, cerr in course_errors:
                    msg += "<li>{0}: <pre>{1}</pre>".format(cmsg, escape(cerr))
                msg += '</ul>'
            except Exception as err:  # pylint: disable=broad-except
                msg += '<br/><p>Error: {0}</p>'.format(escape(err))

    if action == 'Dump list of enrolled students' or action == 'List enrolled students':
        log.debug(action)
        datatable = get_student_grade_summary_data(request, course, get_grades=False, use_offline=use_offline)
        datatable['title'] = _('List of students enrolled in {course_key}').format(course_key=course_key.to_deprecated_string())
        track.views.server_track(request, "list-students", {}, page="idashboard")

    elif 'Dump Grades' in action:
        log.debug(action)
        datatable = get_student_grade_summary_data(request, course, get_grades=True, use_offline=use_offline)
        datatable['title'] = _('Summary Grades of students enrolled in {course_key}').format(course_key=course_key.to_deprecated_string())
        track.views.server_track(request, "dump-grades", {}, page="idashboard")

    elif 'Dump all RAW grades' in action:
        log.debug(action)
        datatable = get_student_grade_summary_data(request, course, get_grades=True,
                                                   get_raw_scores=True, use_offline=use_offline)
        datatable['title'] = _('Raw Grades of students enrolled in {course_key}').format(course_key=course_key)
        track.views.server_track(request, "dump-grades-raw", {}, page="idashboard")

    elif 'Download CSV of all student grades' in action:
        track.views.server_track(request, "dump-grades-csv", {}, page="idashboard")
        return return_csv('grades_{0}.csv'.format(course_key.to_deprecated_string()),
                          get_student_grade_summary_data(request, course, use_offline=use_offline))

    elif 'Download CSV of all RAW grades' in action:
        track.views.server_track(request, "dump-grades-csv-raw", {}, page="idashboard")
        return return_csv('grades_{0}_raw.csv'.format(course_key.to_deprecated_string()),
                          get_student_grade_summary_data(request, course, get_raw_scores=True, use_offline=use_offline))

    elif 'Download CSV of answer distributions' in action:
        track.views.server_track(request, "dump-answer-dist-csv", {}, page="idashboard")
        return return_csv('answer_dist_{0}.csv'.format(course_key.to_deprecated_string()), get_answers_distribution(request, course_key))

    elif 'Dump description of graded assignments configuration' in action:
        # what is "graded assignments configuration"?
        track.views.server_track(request, "dump-graded-assignments-config", {}, page="idashboard")
        msg += dump_grading_context(course)

    elif "Rescore ALL students' problem submissions" in action:
        problem_location_str = strip_if_string(request.POST.get('problem_for_all_students', ''))
        try:
            problem_location = course_key.make_usage_key_from_deprecated_string(problem_location_str)
            instructor_task = submit_rescore_problem_for_all_students(request, problem_location)
            if instructor_task is None:
                msg += '<font color="red">{text}</font>'.format(
                    text=_('Failed to create a background task for rescoring "{problem_url}".').format(
                        problem_url=problem_location_str
                    )
                )
            else:
                track.views.server_track(
                    request,
                    "rescore-all-submissions",
                    {
                        "problem": problem_location_str,
                        "course": course_key.to_deprecated_string()
                    },
                    page="idashboard"
                )

        except (InvalidKeyError, ItemNotFoundError) as err:
            msg += '<font color="red">{text}</font>'.format(
                text=_('Failed to create a background task for rescoring "{problem_url}": problem not found.').format(
                    problem_url=problem_location_str
                )
            )
        except Exception as err:  # pylint: disable=broad-except
            log.error("Encountered exception from rescore: {0}".format(err))
            msg += '<font color="red">{text}</font>'.format(
                text=_('Failed to create a background task for rescoring "{url}": {message}.').format(
                    url=problem_location_str, message=err.message
                )
            )

    elif "Reset ALL students' attempts" in action:
        problem_location_str = strip_if_string(request.POST.get('problem_for_all_students', ''))
        try:
            problem_location = course_key.make_usage_key_from_deprecated_string(problem_location_str)
            instructor_task = submit_reset_problem_attempts_for_all_students(request, problem_location)
            if instructor_task is None:
                msg += '<font color="red">{text}</font>'.format(
                    text=_('Failed to create a background task for resetting "{problem_url}".').format(problem_url=problem_location_str)
                )
            else:
                track.views.server_track(
                    request,
                    "reset-all-attempts",
                    {
                        "problem": problem_location_str,
                        "course": course_key.to_deprecated_string()
                    },
                    page="idashboard"
                )
        except (InvalidKeyError, ItemNotFoundError) as err:
            log.error('Failure to reset: unknown problem "{0}"'.format(err))
            msg += '<font color="red">{text}</font>'.format(
                text=_('Failed to create a background task for resetting "{problem_url}": problem not found.').format(
                    problem_url=problem_location_str
                )
            )
        except Exception as err:  # pylint: disable=broad-except
            log.error("Encountered exception from reset: {0}".format(err))
            msg += '<font color="red">{text}</font>'.format(
                text=_('Failed to create a background task for resetting "{url}": {message}.').format(
                    url=problem_location_str, message=err.message
                )
            )

    elif "Show Background Task History for Student" in action:
        # put this before the non-student case, since the use of "in" will cause this to be missed
        unique_student_identifier = request.POST.get('unique_student_identifier', '')
        message, student = get_student_from_identifier(unique_student_identifier)
        if student is None:
            msg += message
        else:
            problem_location_str = strip_if_string(request.POST.get('problem_for_student', ''))
            try:
                problem_location = course_key.make_usage_key_from_deprecated_string(problem_location_str)
            except InvalidKeyError:
                msg += '<font color="red">{text}</font>'.format(
                    text=_('Could not find problem location "{url}".').format(
                        url=problem_location_str
                    )
                )
            else:
                message, datatable = get_background_task_table(course_key, problem_location, student)
                msg += message

    elif "Show Background Task History" in action:
        problem_location = strip_if_string(request.POST.get('problem_for_all_students', ''))
        try:
            problem_location = course_key.make_usage_key_from_deprecated_string(problem_location_str)
        except InvalidKeyError:
            msg += '<font color="red">{text}</font>'.format(
                text=_('Could not find problem location "{url}".').format(
                    url=problem_location_str
                )
            )
        else:
            message, datatable = get_background_task_table(course_key, problem_location)
            msg += message

    elif ("Reset student's attempts" in action or
          "Delete student state for module" in action or
          "Rescore student's problem submission" in action):
        # get the form data
        unique_student_identifier = request.POST.get(
            'unique_student_identifier', ''
        )
        problem_location_str = strip_if_string(request.POST.get('problem_for_student', ''))
        try:
            module_state_key = course_key.make_usage_key_from_deprecated_string(problem_location_str)
        except InvalidKeyError:
            msg += '<font color="red">{text}</font>'.format(
                text=_('Could not find problem location "{url}".').format(
                    url=problem_location_str
                )
            )
        else:
            # try to uniquely id student by email address or username
            message, student = get_student_from_identifier(unique_student_identifier)
            msg += message
            student_module = None
            if student is not None:
                # Reset the student's score in the submissions API
                # Currently this is used only by open assessment (ORA 2)
                # We need to do this *before* retrieving the `StudentModule` model,
                # because it's possible for a score to exist even if no student module exists.
                if "Delete student state for module" in action:
                    try:
                        sub_api.reset_score(
                            anonymous_id_for_user(student, course_key),
                            course_key.to_deprecated_string(),
                            module_state_key.to_deprecated_string(),
                        )
                    except sub_api.SubmissionError:
                        # Trust the submissions API to log the error
                        error_msg = _("An error occurred while deleting the score.")
                        msg += "<font color='red'>{err}</font>  ".format(err=error_msg)

                # find the module in question
                try:
                    student_module = StudentModule.objects.get(
                        student_id=student.id,
                        course_id=course_key,
                        module_state_key=module_state_key
                    )
                    msg += _("Found module.  ")

                except StudentModule.DoesNotExist as err:
                    error_msg = _("Couldn't find module with that urlname: {url}. ").format(url=problem_location_str)
                    msg += "<font color='red'>{err_msg} ({err})</font>".format(err_msg=error_msg, err=err)
                    log.debug(error_msg)

            if student_module is not None:
                if "Delete student state for module" in action:
                    # delete the state
                    try:
                        student_module.delete()

                        msg += "<font color='red'>{text}</font>".format(
                            text=_("Deleted student module state for {state}!").format(state=module_state_key)
                        )
                        event = {
                            "problem": problem_location_str,
                            "student": unique_student_identifier,
                            "course": course_key.to_deprecated_string()
                        }
                        track.views.server_track(
                            request,
                            "delete-student-module-state",
                            event,
                            page="idashboard"
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        error_msg = _("Failed to delete module state for {id}/{url}. ").format(
                            id=unique_student_identifier, url=problem_location_str
                        )
                        msg += "<font color='red'>{err_msg} ({err})</font>".format(err_msg=error_msg, err=err)
                        log.exception(error_msg)
                elif "Reset student's attempts" in action:
                    # modify the problem's state
                    try:
                        # load the state json
                        problem_state = json.loads(student_module.state)
                        old_number_of_attempts = problem_state["attempts"]
                        problem_state["attempts"] = 0
                        # save
                        student_module.state = json.dumps(problem_state)
                        student_module.save()
                        event = {
                            "old_attempts": old_number_of_attempts,
                            "student": unicode(student),
                            "problem": student_module.module_state_key,
                            "instructor": unicode(request.user),
                            "course": course_key.to_deprecated_string()
                        }
                        track.views.server_track(request, "reset-student-attempts", event, page="idashboard")
                        msg += "<font color='green'>{text}</font>".format(
                            text=_("Module state successfully reset!")
                        )
                    except Exception as err:  # pylint: disable=broad-except
                        error_msg = _("Couldn't reset module state for {id}/{url}. ").format(
                            id=unique_student_identifier, url=problem_location_str
                        )
                        msg += "<font color='red'>{err_msg} ({err})</font>".format(err_msg=error_msg, err=err)
                        log.exception(error_msg)
                else:
                    # "Rescore student's problem submission" case
                    try:
                        instructor_task = submit_rescore_problem_for_student(request, module_state_key, student)
                        if instructor_task is None:
                            msg += '<font color="red">{text}</font>'.format(
                                text=_('Failed to create a background task for rescoring "{key}" for student {id}.').format(
                                    key=module_state_key, id=unique_student_identifier
                                )
                            )
                        else:
                            track.views.server_track(
                                request,
                                "rescore-student-submission",
                                {
                                    "problem": module_state_key,
                                    "student": unique_student_identifier,
                                    "course": course_key.to_deprecated_string()
                                },
                                page="idashboard"
                            )
                    except Exception as err:  # pylint: disable=broad-except
                        msg += '<font color="red">{text}</font>'.format(
                            text=_('Failed to create a background task for rescoring "{key}": {id}.').format(
                                key=module_state_key, id=err.message
                            )
                        )
                        log.exception("Encountered exception from rescore: student '{0}' problem '{1}'".format(
                            unique_student_identifier, module_state_key
                        )
                        )

    elif "Get link to student's progress page" in action:
        unique_student_identifier = request.POST.get('unique_student_identifier', '')
        # try to uniquely id student by email address or username
        message, student = get_student_from_identifier(unique_student_identifier)
        msg += message
        if student is not None:
            progress_url = reverse('student_progress', kwargs={
                'course_id': course_key.to_deprecated_string(),
                'student_id': student.id
            })
            track.views.server_track(
                request,
                "get-student-progress-page",
                {
                    "student": unicode(student),
                    "instructor": unicode(request.user),
                    "course": course_key.to_deprecated_string()
                },
                page="idashboard"
            )
            msg += "<a href='{url}' target='_blank'>{text}</a>.".format(
                url=progress_url,
                text=_("Progress page for username: {username} with email address: {email}").format(
                    username=student.username, email=student.email
                )
            )

    #----------------------------------------
    # export grades to remote gradebook

    elif action == 'List assignments available in remote gradebook':
        msg2, datatable = _do_remote_gradebook(request.user, course, 'get-assignments')
        msg += msg2

    elif action == 'List assignments available for this course':
        log.debug(action)
        allgrades = get_student_grade_summary_data(request, course, get_grades=True, use_offline=use_offline)

        assignments = [[x] for x in allgrades['assignments']]
        datatable = {'header': [_('Assignment Name')]}
        datatable['data'] = assignments
        datatable['title'] = action

        msg += 'assignments=<pre>%s</pre>' % assignments

    elif action == 'List enrolled students matching remote gradebook':
        stud_data = get_student_grade_summary_data(request, course, get_grades=False, use_offline=use_offline)
        msg2, rg_stud_data = _do_remote_gradebook(request.user, course, 'get-membership')
        datatable = {'header': ['Student  email', 'Match?']}
        rg_students = [x['email'] for x in rg_stud_data['retdata']]

        def domatch(x):
            return 'yes' if x.email in rg_students else 'No'
        datatable['data'] = [[x.email, domatch(x)] for x in stud_data['students']]
        datatable['title'] = action

    elif action in ['Display grades for assignment', 'Export grades for assignment to remote gradebook',
                    'Export CSV file of grades for assignment']:

        log.debug(action)
        datatable = {}
        aname = request.POST.get('assignment_name', '')
        if not aname:
            msg += "<font color='red'>{text}</font>".format(text=_("Please enter an assignment name"))
        else:
            allgrades = get_student_grade_summary_data(request, course, get_grades=True, use_offline=use_offline)
            if aname not in allgrades['assignments']:
                msg += "<font color='red'>{text}</font>".format(
                    text=_("Invalid assignment name '{name}'").format(name=aname)
                )
            else:
                aidx = allgrades['assignments'].index(aname)
                datatable = {'header': [_('External email'), aname]}
                ddata = []
                for student in allgrades['students']:  # do one by one in case there is a student who has only partial grades
                    try:
                        ddata.append([student.email, student.grades[aidx]])
                    except IndexError:
                        log.debug('No grade for assignment {idx} ({name}) for student {email}'.format(
                            idx=aidx, name=aname, email=student.email)
                        )
                datatable['data'] = ddata

                datatable['title'] = _('Grades for assignment "{name}"').format(name=aname)

                if 'Export CSV' in action:
                    # generate and return CSV file
                    return return_csv('grades {name}.csv'.format(name=aname), datatable)

                elif 'remote gradebook' in action:
                    file_pointer = StringIO()
                    return_csv('', datatable, file_pointer=file_pointer)
                    file_pointer.seek(0)
                    files = {'datafile': file_pointer}
                    msg2, __ = _do_remote_gradebook(request.user, course, 'post-grades', files=files)
                    msg += msg2

    #----------------------------------------
    # Admin

    elif 'List course staff' in action:
        role = CourseStaffRole(course.id)
        datatable = _role_members_table(role, _("List of Staff"), course_key)
        track.views.server_track(request, "list-staff", {}, page="idashboard")

    elif 'List course instructors' in action and GlobalStaff().has_user(request.user):
        role = CourseInstructorRole(course.id)
        datatable = _role_members_table(role, _("List of Instructors"), course_key)
        track.views.server_track(request, "list-instructors", {}, page="idashboard")

    elif action == 'Add course staff':
        uname = request.POST['staffuser']
        role = CourseStaffRole(course.id)
        msg += add_user_to_role(request, uname, role, 'staff', 'staff')

    elif action == 'Add instructor' and request.user.is_staff:
        uname = request.POST['instructor']
        role = CourseInstructorRole(course.id)
        msg += add_user_to_role(request, uname, role, 'instructor', 'instructor')

    elif action == 'Remove course staff':
        uname = request.POST['staffuser']
        role = CourseStaffRole(course.id)
        msg += remove_user_from_role(request, uname, role, 'staff', 'staff')

    elif action == 'Remove instructor' and request.user.is_staff:
        uname = request.POST['instructor']
        role = CourseInstructorRole(course.id)
        msg += remove_user_from_role(request, uname, role, 'instructor', 'instructor')

    #----------------------------------------
    # DataDump

    elif 'Download CSV of all student profile data' in action:
        enrolled_students = User.objects.filter(
            courseenrollment__course_id=course_key,
            courseenrollment__is_active=1,
        ).order_by('username').select_related("profile")
        profkeys = ['name', 'language', 'location', 'year_of_birth', 'gender', 'level_of_education',
                    'mailing_address', 'goals']
        datatable = {'header': ['username', 'email'] + profkeys}

        def getdat(user):
            """
            Return a list of profile data for the given user.
            """
            profile = user.profile
            return [user.username, user.email] + [getattr(profile, xkey, '') for xkey in profkeys]

        datatable['data'] = [getdat(u) for u in enrolled_students]
        datatable['title'] = _('Student profile data for course {course_id}').format(
            course_id=course_key.to_deprecated_string()
        )
        return return_csv(
            'profiledata_{course_id}.csv'.format(course_id=course_key.to_deprecated_string()),
            datatable
        )

    elif 'Download CSV of all responses to problem' in action:
        problem_to_dump = request.POST.get('problem_to_dump', '')

        if problem_to_dump[-4:] == ".xml":
            problem_to_dump = problem_to_dump[:-4]
        try:
            module_state_key = course_key.make_usage_key(block_type='problem', name=problem_to_dump)
            smdat = StudentModule.objects.filter(
                course_id=course_key,
                module_state_key=module_state_key
            )
            smdat = smdat.order_by('student')
            msg += _("Found {num} records to dump.").format(num=smdat)
        except Exception as err:  # pylint: disable=broad-except
            msg += "<font color='red'>{text}</font><pre>{err}</pre>".format(
                text=_("Couldn't find module with that urlname."),
                err=escape(err)
            )
            smdat = []

        if smdat:
            datatable = {'header': ['username', 'state']}
            datatable['data'] = [[x.student.username, x.state] for x in smdat]
            datatable['title'] = _('Student state for problem {problem}').format(problem=problem_to_dump)
            return return_csv('student_state_from_{problem}.csv'.format(problem=problem_to_dump), datatable)

    elif 'Download CSV of all student anonymized IDs' in action:
        students = User.objects.filter(
            courseenrollment__course_id=course_key,
        ).order_by('id')

        datatable = {'header': ['User ID', 'Anonymized user ID', 'Course Specific Anonymized user ID']}
        datatable['data'] = [[s.id, unique_id_for_user(s), anonymous_id_for_user(s, course_id)] for s in students]
        return return_csv(course_key.to_deprecated_string().replace('/', '-') + '-anon-ids.csv', datatable)

    #----------------------------------------
    # Group management

    elif 'List beta testers' in action:
        role = CourseBetaTesterRole(course.id)
        datatable = _role_members_table(role, _("List of Beta Testers"), course_key)
        track.views.server_track(request, "list-beta-testers", {}, page="idashboard")

    elif action == 'Add beta testers':
        users = request.POST['betausers']
        log.debug("users: {0!r}".format(users))
        role = CourseBetaTesterRole(course.id)
        for username_or_email in split_by_comma_and_whitespace(users):
            msg += "<p>{0}</p>".format(
                add_user_to_role(request, username_or_email, role, 'beta testers', 'beta-tester'))

    elif action == 'Remove beta testers':
        users = request.POST['betausers']
        role = CourseBetaTesterRole(course.id)
        for username_or_email in split_by_comma_and_whitespace(users):
            msg += "<p>{0}</p>".format(
                remove_user_from_role(request, username_or_email, role, 'beta testers', 'beta-tester'))

    #----------------------------------------
    # forum administration

    elif action == 'List course forum admins':
        rolename = FORUM_ROLE_ADMINISTRATOR
        datatable = {}
        msg += _list_course_forum_members(course_key, rolename, datatable)
        track.views.server_track(
            request, "list-forum-admins", {"course": course_key.to_deprecated_string()}, page="idashboard"
        )

    elif action == 'Remove forum admin':
        uname = request.POST['forumadmin']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_ADMINISTRATOR, FORUM_ROLE_REMOVE)
        track.views.server_track(
            request, "remove-forum-admin", {"username": uname, "course": course_key.to_deprecated_string()},
            page="idashboard"
        )

    elif action == 'Add forum admin':
        uname = request.POST['forumadmin']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_ADMINISTRATOR, FORUM_ROLE_ADD)
        track.views.server_track(
            request, "add-forum-admin", {"username": uname, "course": course_key.to_deprecated_string()},
            page="idashboard"
        )

    elif action == 'List course forum moderators':
        rolename = FORUM_ROLE_MODERATOR
        datatable = {}
        msg += _list_course_forum_members(course_key, rolename, datatable)
        track.views.server_track(
            request, "list-forum-mods", {"course": course_key.to_deprecated_string()}, page="idashboard"
        )

    elif action == 'Remove forum moderator':
        uname = request.POST['forummoderator']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_MODERATOR, FORUM_ROLE_REMOVE)
        track.views.server_track(
            request, "remove-forum-mod", {"username": uname, "course": course_key.to_deprecated_string()},
            page="idashboard"
        )

    elif action == 'Add forum moderator':
        uname = request.POST['forummoderator']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_MODERATOR, FORUM_ROLE_ADD)
        track.views.server_track(
            request, "add-forum-mod", {"username": uname, "course": course_key.to_deprecated_string()},
            page="idashboard"
        )

    elif action == 'List course forum community TAs':
        rolename = FORUM_ROLE_COMMUNITY_TA
        datatable = {}
        msg += _list_course_forum_members(course_key, rolename, datatable)
        track.views.server_track(
            request, "list-forum-community-TAs", {"course": course_key.to_deprecated_string()},
            page="idashboard"
        )

    elif action == 'Remove forum community TA':
        uname = request.POST['forummoderator']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_COMMUNITY_TA, FORUM_ROLE_REMOVE)
        track.views.server_track(
            request, "remove-forum-community-TA", {
                "username": uname, "course": course_key.to_deprecated_string()
            },
            page="idashboard"
        )

    elif action == 'Add forum community TA':
        uname = request.POST['forummoderator']
        msg += _update_forum_role_membership(uname, course, FORUM_ROLE_COMMUNITY_TA, FORUM_ROLE_ADD)
        track.views.server_track(
            request, "add-forum-community-TA", {
                "username": uname, "course": course_key.to_deprecated_string()
            },
            page="idashboard"
        )

    #----------------------------------------
    # enrollment

    elif action == 'List students who may enroll but may not have yet signed up':
        ceaset = CourseEnrollmentAllowed.objects.filter(course_id=course_key)
        datatable = {'header': ['StudentEmail']}
        datatable['data'] = [[x.email] for x in ceaset]
        datatable['title'] = action

    elif action == 'Enroll multiple students':

        is_shib_course = uses_shib(course)
        students = request.POST.get('multiple_students', '')
        auto_enroll = bool(request.POST.get('auto_enroll'))
        email_students = bool(request.POST.get('email_students'))
        ret = _do_enroll_students(course, course_key, students, auto_enroll=auto_enroll, email_students=email_students, is_shib_course=is_shib_course)
        datatable = ret['datatable']

    elif action == 'Unenroll multiple students':

        students = request.POST.get('multiple_students', '')
        email_students = bool(request.POST.get('email_students'))
        ret = _do_unenroll_students(course_key, students, email_students=email_students)
        datatable = ret['datatable']

    elif action == 'List sections available in remote gradebook':

        msg2, datatable = _do_remote_gradebook(request.user, course, 'get-sections')
        msg += msg2

    elif action in ['List students in section in remote gradebook',
                    'Overload enrollment list using remote gradebook',
                    'Merge enrollment list with remote gradebook']:

        section = request.POST.get('gradebook_section', '')
        msg2, datatable = _do_remote_gradebook(request.user, course, 'get-membership', dict(section=section))
        msg += msg2

        if not 'List' in action:
            students = ','.join([x['email'] for x in datatable['retdata']])
            overload = 'Overload' in action
            ret = _do_enroll_students(course, course_key, students, overload=overload)
            datatable = ret['datatable']

    #----------------------------------------
    # email

    elif action == 'Send email':
        email_to_option = request.POST.get("to_option")
        email_subject = request.POST.get("subject")
        html_message = request.POST.get("message")

        if bulk_email_is_enabled_for_course(course_key):
            try:
                # Create the CourseEmail object.  This is saved immediately, so that
                # any transaction that has been pending up to this point will also be
                # committed.
                email = CourseEmail.create(
                    course_key.to_deprecated_string(), request.user, email_to_option, email_subject, html_message
                )

                # Submit the task, so that the correct InstructorTask object gets created (for monitoring purposes)
                submit_bulk_course_email(request, course_key, email.id)  # pylint: disable=E1101

            except Exception as err:  # pylint: disable=broad-except
                # Catch any errors and deliver a message to the user
                error_msg = "Failed to send email! ({0})".format(err)
                msg += "<font color='red'>" + error_msg + "</font>"
                log.exception(error_msg)

            else:
                # If sending the task succeeds, deliver a success message to the user.
                if email_to_option == "all":
                    text = _(
                        "Your email was successfully queued for sending. "
                        "Please note that for large classes, it may take up to an hour "
                        "(or more, if other courses are simultaneously sending email) "
                        "to send all emails."
                    )
                else:
                    text = _('Your email was successfully queued for sending.')
                email_msg = '<div class="msg msg-confirm"><p class="copy">{text}</p></div>'.format(text=text)
        else:
            msg += "<font color='red'>Email is not enabled for this course.</font>"

    elif "Show Background Email Task History" in action:
        message, datatable = get_background_task_table(course_key, task_type='bulk_course_email')
        msg += message

    elif "Show Background Email Task History" in action:
        message, datatable = get_background_task_table(course_key, task_type='bulk_course_email')
        msg += message

    #----------------------------------------
    # psychometrics

    elif action == 'Generate Histogram and IRT Plot':
        problem = request.POST['Problem']
        nmsg, plots = psychoanalyze.generate_plots_for_problem(problem)
        msg += nmsg
        track.views.server_track(request, "psychometrics-histogram-generation", {"problem": unicode(problem)}, page="idashboard")

    if idash_mode == 'Psychometrics':
        problems = psychoanalyze.problems_with_psychometric_data(course_key)

    #----------------------------------------
    # analytics
    def get_analytics_result(analytics_name):
        """Return data for an Analytic piece, or None if it doesn't exist. It
        logs and swallows errors.
        """
        url = settings.ANALYTICS_SERVER_URL + \
            u"get?aname={}&course_id={}&apikey={}".format(
                analytics_name, course_key.to_deprecated_string(), settings.ANALYTICS_API_KEY
            )
        try:
            res = requests.get(url)
        except Exception:  # pylint: disable=broad-except
            log.exception("Error trying to access analytics at %s", url)
            return None

        if res.status_code == codes.OK:
            # WARNING: do not use req.json because the preloaded json doesn't
            # preserve the order of the original record (hence OrderedDict).
            return json.loads(res.content, object_pairs_hook=OrderedDict)
        else:
            log.error("Error fetching %s, code: %s, msg: %s",
                      url, res.status_code, res.content)
        return None

    analytics_results = {}

    if idash_mode == 'Analytics':
        DASHBOARD_ANALYTICS = [
            # "StudentsAttemptedProblems",  # num students who tried given problem
            "StudentsDailyActivity",  # active students by day
            "StudentsDropoffPerDay",  # active students dropoff by day
            # "OverallGradeDistribution",  # overall point distribution for course
            "StudentsActive",  # num students active in time period (default = 1wk)
            "StudentsEnrolled",  # num students enrolled
            # "StudentsPerProblemCorrect",  # foreach problem, num students correct
            "ProblemGradeDistribution",  # foreach problem, grade distribution
        ]
        for analytic_name in DASHBOARD_ANALYTICS:
            analytics_results[analytic_name] = get_analytics_result(analytic_name)

    #----------------------------------------
    # Metrics

    metrics_results = {}
    if settings.FEATURES.get('CLASS_DASHBOARD') and idash_mode == 'Metrics':
        metrics_results['section_display_name'] = dashboard_data.get_section_display_name(course_key)
        metrics_results['section_has_problem'] = dashboard_data.get_array_section_has_problem(course_key)

    #----------------------------------------
    # offline grades?

    if use_offline:
        msg += "<br/><font color='orange'>{text}</font>".format(
            text=_("Grades from {course_id}").format(
                course_id=offline_grades_available(course_key)
            )
        )

    # generate list of pending background tasks
    if settings.FEATURES.get('ENABLE_INSTRUCTOR_BACKGROUND_TASKS'):
        instructor_tasks = get_running_instructor_tasks(course_key)
    else:
        instructor_tasks = None

    # determine if this is a studio-backed course so we can provide a link to edit this course in studio
    is_studio_course = modulestore().get_modulestore_type(course_key) != XML_MODULESTORE_TYPE
    studio_url = None
    if is_studio_course:
        studio_url = get_cms_course_link(course)

    email_editor = None
    # HTML editor for email
    if idash_mode == 'Email' and is_studio_course:
        html_module = HtmlDescriptor(
            course.system,
            DictFieldData({'data': html_message}),
            ScopeIds(None, None, None, course_key.make_usage_key('html', 'dummy'))
        )
        fragment = html_module.render('studio_view')
        fragment = wrap_xblock(
            'LmsRuntime', html_module, 'studio_view', fragment, None,
            extra_data={"course-id": course_key.to_deprecated_string()},
            usage_id_serializer=lambda usage_id: quote_slashes(usage_id.to_deprecated_string())
        )
        email_editor = fragment.content

    # Enable instructor email only if the following conditions are met:
    # 1. Feature flag is on
    # 2. We have explicitly enabled email for the given course via django-admin
    # 3. It is NOT an XML course
    if bulk_email_is_enabled_for_course(course_key):
        show_email_tab = True

    # display course stats only if there is no other table to display:
    course_stats = None
    if not datatable:
        course_stats = get_course_stats_table()

    # disable buttons for large courses
    disable_buttons = False
    max_enrollment_for_buttons = settings.FEATURES.get("MAX_ENROLLMENT_INSTR_BUTTONS")
    if max_enrollment_for_buttons is not None:
        disable_buttons = enrollment_number > max_enrollment_for_buttons

    #----------------------------------------
    # context for rendering

    context = {
        'course': course,
        'staff_access': True,
        'admin_access': request.user.is_staff,
        'instructor_access': instructor_access,
        'forum_admin_access': forum_admin_access,
        'datatable': datatable,
        'course_stats': course_stats,
        'msg': msg,
        'modeflag': {idash_mode: 'selectedmode'},
        'studio_url': studio_url,

        'to_option': email_to_option,      # email
        'subject': email_subject,          # email
        'editor': email_editor,            # email
        'email_msg': email_msg,            # email
        'show_email_tab': show_email_tab,  # email

        'problems': problems,  # psychometrics
        'plots': plots,  # psychometrics
        'course_errors': modulestore().get_course_errors(course.id),
        'instructor_tasks': instructor_tasks,
        'offline_grade_log': offline_grades_available(course_key),
        'cohorts_ajax_url': reverse('cohorts', kwargs={'course_key': course_key.to_deprecated_string()}),

        'analytics_results': analytics_results,
        'disable_buttons': disable_buttons,
        'metrics_results': metrics_results,
    }

    context['standard_dashboard_url'] = reverse('instructor_dashboard', kwargs={'course_id': course_key.to_deprecated_string()})

    return render_to_response('courseware/instructor_dashboard.html', context)


def _do_remote_gradebook(user, course, action, args=None, files=None):
    '''
    Perform remote gradebook action.  Returns msg, datatable.
    '''
    rg = course.remote_gradebook
    if not rg:
        msg = _("No remote gradebook defined in course metadata")
        return msg, {}

    rgurl = settings.FEATURES.get('REMOTE_GRADEBOOK_URL', '')
    if not rgurl:
        msg = _("No remote gradebook url defined in settings.FEATURES")
        return msg, {}

    rgname = rg.get('name', '')
    if not rgname:
        msg = _("No gradebook name defined in course remote_gradebook metadata")
        return msg, {}

    if args is None:
        args = {}
    data = dict(submit=action, gradebook=rgname, user=user.email)
    data.update(args)

    try:
        resp = requests.post(rgurl, data=data, verify=False, files=files)
        retdict = json.loads(resp.content)
    except Exception as err:  # pylint: disable=broad-except
        msg = _("Failed to communicate with gradebook server at {url}").format(url=rgurl) + "<br/>"
        msg += _("Error: {err}").format(err=err)
        msg += "<br/>resp={resp}".format(resp=resp.content)
        msg += "<br/>data={data}".format(data=data)
        return msg, {}

    msg = '<pre>{msg}</pre>'.format(msg=retdict['msg'].replace('\n', '<br/>'))
    retdata = retdict['data']  # a list of dicts

    if retdata:
        datatable = {'header': retdata[0].keys()}
        datatable['data'] = [x.values() for x in retdata]
        datatable['title'] = _('Remote gradebook response for {action}').format(action=action)
        datatable['retdata'] = retdata
    else:
        datatable = {}

    return msg, datatable


def _list_course_forum_members(course_key, rolename, datatable):
    """
    Fills in datatable with forum membership information, for a given role,
    so that it will be displayed on instructor dashboard.

      course_ID = the CourseKey for a course
      rolename = one of "Administrator", "Moderator", "Community TA"

    Returns message status string to append to displayed message, if role is unknown.
    """
    # make sure datatable is set up properly for display first, before checking for errors
    datatable['header'] = [_('Username'), _('Full name'), _('Roles')]
    datatable['title'] = _('List of Forum {name}s in course {id}').format(
        name=rolename, id=course_key.to_deprecated_string()
    )
    datatable['data'] = []
    try:
        role = Role.objects.get(name=rolename, course_id=course_key)
    except Role.DoesNotExist:
        return '<font color="red">' + _('Error: unknown rolename "{rolename}"').format(rolename=rolename) + '</font>'
    uset = role.users.all().order_by('username')
    msg = 'Role = {0}'.format(rolename)
    log.debug('role={0}'.format(rolename))
    datatable['data'] = [[x.username, x.profile.name, ', '.join([
        r.name for r in x.roles.filter(course_id=course_key).order_by('name')
    ])] for x in uset]
    return msg


def _update_forum_role_membership(uname, course, rolename, add_or_remove):
    '''
    Supports adding a user to a course's forum role

      uname = username string for user
      course = course object
      rolename = one of "Administrator", "Moderator", "Community TA"
      add_or_remove = one of "add" or "remove"

    Returns message status string to append to displayed message,  Status is returned if user
    or role is unknown, or if entry already exists when adding, or if entry doesn't exist when removing.
    '''
    # check that username and rolename are valid:
    try:
        user = User.objects.get(username=uname)
    except User.DoesNotExist:
        return '<font color="red">' + _('Error: unknown username "{username}"').format(username=uname) + '</font>'
    try:
        role = Role.objects.get(name=rolename, course_id=course.id)
    except Role.DoesNotExist:
        return '<font color="red">' + _('Error: unknown rolename "{rolename}"').format(rolename=rolename) + '</font>'

    # check whether role already has the specified user:
    alreadyexists = role.users.filter(username=uname).exists()
    msg = ''
    log.debug('rolename={0}'.format(rolename))
    if add_or_remove == FORUM_ROLE_REMOVE:
        if not alreadyexists:
            msg = '<font color="red">' + _('Error: user "{username}" does not have rolename "{rolename}", cannot remove').format(username=uname, rolename=rolename) + '</font>'
        else:
            user.roles.remove(role)
            msg = '<font color="green">' + _('Removed "{username}" from "{course_id}" forum role = "{rolename}"').format(username=user, course_id=course.id.to_deprecated_string(), rolename=rolename) + '</font>'
    else:
        if alreadyexists:
            msg = '<font color="red">' + _('Error: user "{username}" already has rolename "{rolename}", cannot add').format(username=uname, rolename=rolename) + '</font>'
        else:
            if (rolename == FORUM_ROLE_ADMINISTRATOR and not has_access(user, 'staff', course)):
                msg = '<font color="red">' + _('Error: user "{username}" should first be added as staff before adding as a forum administrator, cannot add').format(username=uname) + '</font>'
            else:
                user.roles.add(role)
                msg = '<font color="green">' + _('Added "{username}" to "{course_id}" forum role = "{rolename}"').format(username=user, course_id=course.id.to_deprecated_string(), rolename=rolename) + '</font>'

    return msg


def _role_members_table(role, title, course_key):
    """
    Return a data table of usernames and names of users in group_name.

    Arguments:
        role -- a student.roles.AccessRole
        title -- a descriptive title to show the user

    Returns:
        a dictionary with keys
        'header': ['Username', 'Full name'],
        'data': [[username, name] for all users]
        'title': "{title} in course {course}"
    """
    uset = role.users_with_role()
    datatable = {'header': [_('Username'), _('Full name')]}
    datatable['data'] = [[x.username, x.profile.name] for x in uset]
    datatable['title'] = _('{title} in course {course_key}').format(title=title, course_key=course_key.to_deprecated_string())
    return datatable


def _user_from_name_or_email(username_or_email):
    """
    Return the `django.contrib.auth.User` with the supplied username or email.

    If `username_or_email` contains an `@` it is treated as an email, otherwise
    it is treated as the username
    """
    username_or_email = strip_if_string(username_or_email)

    if '@' in username_or_email:
        return User.objects.get(email=username_or_email)
    else:
        return User.objects.get(username=username_or_email)


def add_user_to_role(request, username_or_email, role, group_title, event_name):
    """
    Look up the given user by username (if no '@') or email (otherwise), and add them to group.

    Arguments:
       request: django request--used for tracking log
       username_or_email: who to add.  Decide if it's an email by presense of an '@'
       group: A group name
       group_title: what to call this group in messages to user--e.g. "beta-testers".
       event_name: what to call this event when logging to tracking logs.

    Returns:
       html to insert in the message field
    """
    username_or_email = strip_if_string(username_or_email)
    try:
        user = _user_from_name_or_email(username_or_email)
    except User.DoesNotExist:
        return u'<font color="red">Error: unknown username or email "{0}"</font>'.format(username_or_email)

    role.add_users(user)

    # Deal with historical event names
    if event_name in ('staff', 'beta-tester'):
        track.views.server_track(
            request,
            "add-or-remove-user-group",
            {
                "event_name": event_name,
                "user": unicode(user),
                "event": "add"
            },
            page="idashboard"
        )
    else:
        track.views.server_track(request, "add-instructor", {"instructor": unicode(user)}, page="idashboard")

    return '<font color="green">Added {0} to {1}</font>'.format(user, group_title)


def remove_user_from_role(request, username_or_email, role, group_title, event_name):
    """
    Look up the given user by username (if no '@') or email (otherwise), and remove them from the supplied role.

    Arguments:
       request: django request--used for tracking log
       username_or_email: who to remove.  Decide if it's an email by presense of an '@'
       role: A student.roles.AccessRole
       group_title: what to call this group in messages to user--e.g. "beta-testers".
       event_name: what to call this event when logging to tracking logs.

    Returns:
       html to insert in the message field
    """

    username_or_email = strip_if_string(username_or_email)
    try:
        user = _user_from_name_or_email(username_or_email)
    except User.DoesNotExist:
        return u'<font color="red">Error: unknown username or email "{0}"</font>'.format(username_or_email)

    role.remove_users(user)

    # Deal with historical event names
    if event_name in ('staff', 'beta-tester'):
        track.views.server_track(
            request,
            "add-or-remove-user-group",
            {
                "event_name": event_name,
                "user": unicode(user),
                "event": "remove"
            },
            page="idashboard"
        )
    else:
        track.views.server_track(request, "remove-instructor", {"instructor": unicode(user)}, page="idashboard")

    return '<font color="green">Removed {0} from {1}</font>'.format(user, group_title)


class GradeTable(object):
    """
    Keep track of grades, by student, for all graded assignment
    components.  Each student's grades are stored in a list.  The
    index of this list specifies the assignment component.  Not
    all lists have the same length, because at the start of going
    through the set of grades, it is unknown what assignment
    compoments exist.  This is because some students may not do
    all the assignment components.

    The student grades are then stored in a dict, with the student
    id as the key.
    """
    def __init__(self):
        self.components = OrderedDict()
        self.grades = {}
        self._current_row = {}

    def _add_grade_to_row(self, component, score):
        """Creates component if needed, and assigns score

        Args:
            component (str): Course component being graded
            score (float): Score of student on component

        Returns:
           None
        """
        component_index = self.components.setdefault(component, len(self.components))
        self._current_row[component_index] = score

    @contextmanager
    def add_row(self, student_id):
        """Context management for a row of grades

        Uses a new dictionary to get all grades of a specified student
        and closes by adding that dict to the internal table.

        Args:
            student_id (str): Student id that is having grades set

        """
        self._current_row = {}
        yield self._add_grade_to_row
        self.grades[student_id] = self._current_row

    def get_grade(self, student_id):
        """Retrieves padded list of grades for specified student

        Args:
            student_id (str): Student ID for desired grades

        Returns:
            list: Ordered list of grades for student

        """
        row = self.grades.get(student_id, [])
        ncomp = len(self.components)
        return [row.get(comp, None) for comp in range(ncomp)]

    def get_graded_components(self):
        """
        Return a list of components that have been
        discovered so far.
        """
        return self.components.keys()


def get_student_grade_summary_data(request, course, get_grades=True, get_raw_scores=False, use_offline=False):
    """
    Return data arrays with student identity and grades for specified course.

    course = CourseDescriptor
    course_key = course ID

    Note: both are passed in, only because instructor_dashboard already has them already.

    returns datatable = dict(header=header, data=data)
    where

    header = list of strings labeling the data fields
    data = list (one per student) of lists of data corresponding to the fields

    If get_raw_scores=True, then instead of grade summaries, the raw grades for all graded modules are returned.
    """
    course_key = course.id
    enrolled_students = User.objects.filter(
        courseenrollment__course_id=course_key,
        courseenrollment__is_active=1,
    ).prefetch_related("groups").order_by('username')

    header = [_('ID'), _('Username'), _('Full Name'), _('edX email'), _('External email')]

    datatable = {'header': header, 'students': enrolled_students}
    data = []

    gtab = GradeTable()

    for student in enrolled_students:
        datarow = [student.id, student.username, student.profile.name, student.email]
        try:
            datarow.append(student.externalauthmap.external_email)
        except:  # ExternalAuthMap.DoesNotExist
            datarow.append('')

        if get_grades:
            gradeset = student_grades(student, request, course, keep_raw_scores=get_raw_scores, use_offline=use_offline)
            log.debug('student={0}, gradeset={1}'.format(student, gradeset))
            with gtab.add_row(student.id) as add_grade:
                if get_raw_scores:
                    # TODO (ichuang) encode Score as dict instead of as list, so score[0] -> score['earned']
                    for score in gradeset['raw_scores']:
                        add_grade(score.section, getattr(score, 'earned', score[0]))
                else:
                    for grade_item in gradeset['section_breakdown']:
                        add_grade(grade_item['label'], grade_item['percent'])
            student.grades = gtab.get_grade(student.id)

        data.append(datarow)

    # if getting grades, need to do a second pass, and add grades to each datarow;
    # on the first pass we don't know all the graded components
    if get_grades:
        for datarow in data:
            # get grades for student
            sgrades = gtab.get_grade(datarow[0])
            datarow += sgrades

        # get graded components and add to table header
        assignments = gtab.get_graded_components()
        header += assignments
        datatable['assignments'] = assignments

    datatable['data'] = data
    return datatable

#-----------------------------------------------------------------------------

# Gradebook has moved to instructor.api.spoc_gradebook #

@cache_control(no_cache=True, no_store=True, must_revalidate=True)
def grade_summary(request, course_key):
    """Display the grade summary for a course."""
    course = get_course_with_access(request.user, 'staff', course_key)

    # For now, just a page
    context = {'course': course,
               'staff_access': True, }
    return render_to_response('courseware/grade_summary.html', context)


#-----------------------------------------------------------------------------
# enrollment

def _do_enroll_students(course, course_key, students, overload=False, auto_enroll=False, email_students=False, is_shib_course=False):
    """
    Do the actual work of enrolling multiple students, presented as a string
    of emails separated by commas or returns
    `course` is course object
    `course_key` id of course (a CourseKey)
    `students` string of student emails separated by commas or returns (a `str`)
    `overload` un-enrolls all existing students (a `boolean`)
    `auto_enroll` is user input preference (a `boolean`)
    `email_students` is user input preference (a `boolean`)
    """

    new_students, new_students_lc = get_and_clean_student_list(students)
    status = dict([x, 'unprocessed'] for x in new_students)

    if overload:  # delete all but staff
        todelete = CourseEnrollment.objects.filter(course_id=course_key)
        for ce in todelete:
            if not has_access(ce.user, 'staff', course) and ce.user.email.lower() not in new_students_lc:
                status[ce.user.email] = 'deleted'
                ce.deactivate()
            else:
                status[ce.user.email] = 'is staff'
        ceaset = CourseEnrollmentAllowed.objects.filter(course_id=course_key)
        for cea in ceaset:
            status[cea.email] = 'removed from pending enrollment list'
        ceaset.delete()

    if email_students:
        stripped_site_name = microsite.get_value(
            'SITE_NAME',
            settings.SITE_NAME
        )
        # TODO: Use request.build_absolute_uri rather than 'https://{}{}'.format
        # and check with the Services team that this works well with microsites
        registration_url = 'https://{}{}'.format(
            stripped_site_name,
            reverse('student.views.register_user')
        )
        course_url = 'https://{}{}'.format(
            stripped_site_name,
            reverse('course_root', kwargs={'course_id': course_key.to_deprecated_string()})
        )
        # We can't get the url to the course's About page if the marketing site is enabled.
        course_about_url = None
        if not settings.FEATURES.get('ENABLE_MKTG_SITE', False):
            course_about_url = u'https://{}{}'.format(
                stripped_site_name,
                reverse('about_course', kwargs={'course_id': course_key.to_deprecated_string()})
            )

        # Composition of email
        d = {
            'site_name': stripped_site_name,
            'registration_url': registration_url,
            'course': course,
            'auto_enroll': auto_enroll,
            'course_url': course_url,
            'course_about_url': course_about_url,
            'is_shib_course': is_shib_course
        }

    for student in new_students:
        try:
            user = User.objects.get(email=student)
        except User.DoesNotExist:

            #Student not signed up yet, put in pending enrollment allowed table
            cea = CourseEnrollmentAllowed.objects.filter(email=student, course_id=course_key)

            #If enrollmentallowed already exists, update auto_enroll flag to however it was set in UI
            #Will be 0 or 1 records as there is a unique key on email + course_id
            if cea:
                cea[0].auto_enroll = auto_enroll
                cea[0].save()
                status[student] = 'user does not exist, enrollment already allowed, pending with auto enrollment ' \
                    + ('on' if auto_enroll else 'off')
                continue

            #EnrollmentAllowed doesn't exist so create it
            cea = CourseEnrollmentAllowed(email=student, course_id=course_key, auto_enroll=auto_enroll)
            cea.save()

            status[student] = 'user does not exist, enrollment allowed, pending with auto enrollment ' \
                + ('on' if auto_enroll else 'off')

            if email_students:
                # User is allowed to enroll but has not signed up yet
                d['email_address'] = student
                d['message'] = 'allowed_enroll'
                send_mail_ret = send_mail_to_student(student, d)
                status[student] += (', email sent' if send_mail_ret else '')
            continue

        # Student has already registered
        if CourseEnrollment.is_enrolled(user, course_key):
            status[student] = 'already enrolled'
            continue

        try:
            # Not enrolled yet
            CourseEnrollment.enroll(user, course_key)
            status[student] = 'added'

            if email_students:
                # User enrolled for first time, populate dict with user specific info
                d['email_address'] = student
                d['full_name'] = user.profile.name
                d['message'] = 'enrolled_enroll'
                send_mail_ret = send_mail_to_student(student, d)
                status[student] += (', email sent' if send_mail_ret else '')

        except:
            status[student] = 'rejected'

    datatable = {'header': ['StudentEmail', 'action']}
    datatable['data'] = [[x, status[x]] for x in sorted(status)]
    datatable['title'] = _('Enrollment of students')

    def sf(stat):
        return [x for x in status if status[x] == stat]

    data = dict(added=sf('added'), rejected=sf('rejected') + sf('exists'),
                deleted=sf('deleted'), datatable=datatable)

    return data


#Unenrollment
def _do_unenroll_students(course_key, students, email_students=False):
    """
    Do the actual work of un-enrolling multiple students, presented as a string
    of emails separated by commas or returns
    `course_key` is id of course (a `str`)
    `students` is string of student emails separated by commas or returns (a `str`)
    `email_students` is user input preference (a `boolean`)
    """

    old_students, __ = get_and_clean_student_list(students)
    status = dict([x, 'unprocessed'] for x in old_students)

    stripped_site_name = microsite.get_value(
        'SITE_NAME',
        settings.SITE_NAME
    )
    if email_students:
        course = course_from_id(course_key)
        #Composition of email
        d = {'site_name': stripped_site_name,
             'course': course}

    for student in old_students:

        isok = False
        cea = CourseEnrollmentAllowed.objects.filter(course_id=course_key, email=student)
        #Will be 0 or 1 records as there is a unique key on email + course_id
        if cea:
            cea[0].delete()
            status[student] = "un-enrolled"
            isok = True

        try:
            user = User.objects.get(email=student)
        except User.DoesNotExist:

            if isok and email_students:
                #User was allowed to join but had not signed up yet
                d['email_address'] = student
                d['message'] = 'allowed_unenroll'
                send_mail_ret = send_mail_to_student(student, d)
                status[student] += (', email sent' if send_mail_ret else '')

            continue

        #Will be 0 or 1 records as there is a unique key on user + course_id
        if CourseEnrollment.is_enrolled(user, course_key):
            try:
                CourseEnrollment.unenroll(user, course_key)
                status[student] = "un-enrolled"
                if email_students:
                    #User was enrolled
                    d['email_address'] = student
                    d['full_name'] = user.profile.name
                    d['message'] = 'enrolled_unenroll'
                    send_mail_ret = send_mail_to_student(student, d)
                    status[student] += (', email sent' if send_mail_ret else '')

            except Exception:  # pylint: disable=broad-except
                if not isok:
                    status[student] = "Error!  Failed to un-enroll"

    datatable = {'header': ['StudentEmail', 'action']}
    datatable['data'] = [[x, status[x]] for x in sorted(status)]
    datatable['title'] = _('Un-enrollment of students')

    data = dict(datatable=datatable)
    return data


def send_mail_to_student(student, param_dict):
    """
    Construct the email using templates and then send it.
    `student` is the student's email address (a `str`),

    `param_dict` is a `dict` with keys [
    `site_name`: name given to edX instance (a `str`)
    `registration_url`: url for registration (a `str`)
    `course_key`: id of course (a CourseKey)
    `auto_enroll`: user input option (a `str`)
    `course_url`: url of course (a `str`)
    `email_address`: email of student (a `str`)
    `full_name`: student full name (a `str`)
    `message`: type of email to send and template to use (a `str`)
    `is_shib_course`: (a `boolean`)
                                        ]
    Returns a boolean indicating whether the email was sent successfully.
    """

    # add some helpers and microconfig subsitutions
    if 'course' in param_dict:
        param_dict['course_name'] = param_dict['course'].display_name_with_default
    param_dict['site_name'] = microsite.get_value(
        'SITE_NAME',
        param_dict.get('site_name', '')
    )

    subject = None
    message = None

    message_type = param_dict['message']

    email_template_dict = {
        'allowed_enroll': ('emails/enroll_email_allowedsubject.txt', 'emails/enroll_email_allowedmessage.txt'),
        'enrolled_enroll': ('emails/enroll_email_enrolledsubject.txt', 'emails/enroll_email_enrolledmessage.txt'),
        'allowed_unenroll': ('emails/unenroll_email_subject.txt', 'emails/unenroll_email_allowedmessage.txt'),
        'enrolled_unenroll': ('emails/unenroll_email_subject.txt', 'emails/unenroll_email_enrolledmessage.txt'),
    }

    subject_template, message_template = email_template_dict.get(message_type, (None, None))
    if subject_template is not None and message_template is not None:
        subject = render_to_string(subject_template, param_dict)
        message = render_to_string(message_template, param_dict)

    if subject and message:
        # Remove leading and trailing whitespace from body
        message = message.strip()

        # Email subject *must not* contain newlines
        subject = ''.join(subject.splitlines())
        from_address = microsite.get_value(
            'email_from_address',
            settings.DEFAULT_FROM_EMAIL
        )

        send_mail(subject, message, from_address, [student], fail_silently=False)

        return True
    else:
        return False


def get_and_clean_student_list(students):
    """
    Separate out individual student email from the comma, or space separated string.
    `students` is string of student emails separated by commas or returns (a `str`)
    Returns:
    students: list of cleaned student emails
    students_lc: list of lower case cleaned student emails
    """

    students = split_by_comma_and_whitespace(students)
    students = [unicode(s.strip()) for s in students]
    students = [s for s in students if s != '']
    students_lc = [x.lower() for x in students]

    return students, students_lc

#-----------------------------------------------------------------------------
# answer distribution


def get_answers_distribution(request, course_key):
    """
    Get the distribution of answers for all graded problems in the course.

    Return a dict with two keys:
    'header': a header row
    'data': a list of rows
    """
    course = get_course_with_access(request.user, 'staff', course_key)

    dist = grades.answer_distributions(course.id)

    d = {}
    d['header'] = ['url_name', 'display name', 'answer id', 'answer', 'count']

    d['data'] = [
        [url_name, display_name, answer_id, a, answers[a]]
        for (url_name, display_name, answer_id), answers in sorted(dist.items())
        for a in answers
    ]
    return d


#-----------------------------------------------------------------------------


def compute_course_stats(course):
    """
    Compute course statistics, including number of problems, videos, html.

    course is a CourseDescriptor from the xmodule system.
    """

    # walk the course by using get_children() until we come to the leaves; count the
    # number of different leaf types

    counts = defaultdict(int)

    def walk(module):
        children = module.get_children()
        category = module.__class__.__name__  # HtmlDescriptor, CapaDescriptor, ...
        counts[category] += 1
        for c in children:
            walk(c)

    walk(course)
    stats = dict(counts)  # number of each kind of module
    return stats


def dump_grading_context(course):
    """
    Dump information about course grading context (eg which problems are graded in what assignments)
    Very useful for debugging grading_policy.json and policy.json
    """
    msg = "-----------------------------------------------------------------------------\n"
    msg += "Course grader:\n"

    msg += '%s\n' % course.grader.__class__
    graders = {}
    if isinstance(course.grader, xmgraders.WeightedSubsectionsGrader):
        msg += '\n'
        msg += "Graded sections:\n"
        for subgrader, category, weight in course.grader.sections:
            msg += "  subgrader=%s, type=%s, category=%s, weight=%s\n" % (subgrader.__class__, subgrader.type, category, weight)
            subgrader.index = 1
            graders[subgrader.type] = subgrader
    msg += "-----------------------------------------------------------------------------\n"
    msg += "Listing grading context for course %s\n" % course.id

    gcontext = course.grading_context
    msg += "graded sections:\n"

    msg += '%s\n' % gcontext['graded_sections'].keys()
    for (gsections, gsvals) in gcontext['graded_sections'].items():
        msg += "--> Section %s:\n" % (gsections)
        for sec in gsvals:
            sdesc = sec['section_descriptor']
            grade_format = getattr(sdesc, 'grade_format', None)
            aname = ''
            if grade_format in graders:
                gfmt = graders[grade_format]
                aname = '%s %02d' % (gfmt.short_label, gfmt.index)
                gfmt.index += 1
            elif sdesc.display_name in graders:
                gfmt = graders[sdesc.display_name]
                aname = '%s' % gfmt.short_label
            notes = ''
            if getattr(sdesc, 'score_by_attempt', False):
                notes = ', score by attempt!'
            msg += "      %s (grade_format=%s, Assignment=%s%s)\n" % (s.display_name, grade_format, aname, notes)
    msg += "all descriptors:\n"
    msg += "length=%d\n" % len(gcontext['all_descriptors'])
    msg = '<pre>%s</pre>' % msg.replace('<', '&lt;')
    return msg


def get_background_task_table(course_key, problem_url=None, student=None, task_type=None):
    """
    Construct the "datatable" structure to represent background task history.

    Filters the background task history to the specified course and problem.
    If a student is provided, filters to only those tasks for which that student
    was specified.

    Returns a tuple of (msg, datatable), where the msg is a possible error message,
    and the datatable is the datatable to be used for display.
    """
    history_entries = get_instructor_task_history(course_key, problem_url, student, task_type)
    datatable = {}
    msg = ""
    # first check to see if there is any history at all
    # (note that we don't have to check that the arguments are valid; it
    # just won't find any entries.)
    if (history_entries.count()) == 0:
        if problem_url is None:
            msg += '<font color="red">Failed to find any background tasks for course "{course}".</font>'.format(
                course=course_key.to_deprecated_string()
            )
        elif student is not None:
            template = '<font color="red">' + _('Failed to find any background tasks for course "{course}", module "{problem}" and student "{student}".') + '</font>'
            msg += template.format(course=course_key.to_deprecated_string(), problem=problem_url, student=student.username)
        else:
            msg += '<font color="red">' + _('Failed to find any background tasks for course "{course}" and module "{problem}".').format(
                course=course_key.to_deprecated_string(), problem=problem_url
            ) + '</font>'
    else:
        datatable['header'] = ["Task Type",
                               "Task Id",
                               "Requester",
                               "Submitted",
                               "Duration (sec)",
                               "Task State",
                               "Task Status",
                               "Task Output"]

        datatable['data'] = []
        for instructor_task in history_entries:
            # get duration info, if known:
            duration_sec = 'unknown'
            if hasattr(instructor_task, 'task_output') and instructor_task.task_output is not None:
                task_output = json.loads(instructor_task.task_output)
                if 'duration_ms' in task_output:
                    duration_sec = int(task_output['duration_ms'] / 1000.0)
            # get progress status message:
            success, task_message = get_task_completion_info(instructor_task)
            status = "Complete" if success else "Incomplete"
            # generate row for this task:
            row = [
                str(instructor_task.task_type),
                str(instructor_task.task_id),
                str(instructor_task.requester),
                instructor_task.created.isoformat(' '),
                duration_sec,
                str(instructor_task.task_state),
                status,
                task_message
            ]
            datatable['data'].append(row)

        if problem_url is None:
            datatable['title'] = "{course_id}".format(course_id=course_key.to_deprecated_string())
        elif student is not None:
            datatable['title'] = "{course_id} > {location} > {student}".format(
                course_id=course_key.to_deprecated_string(),
                location=problem_url,
                student=student.username
            )
        else:
            datatable['title'] = "{course_id} > {location}".format(
                course_id=course_key.to_deprecated_string(), location=problem_url
            )

    return msg, datatable


def uses_shib(course):
    """
    Used to return whether course has Shibboleth as the enrollment domain

    Returns a boolean indicating if Shibboleth authentication is set for this course.
    """
    return course.enrollment_domain and course.enrollment_domain.startswith(SHIBBOLETH_DOMAIN_PREFIX)
