"""
Test cases for tasks.py
"""
from unittest import mock

import ddt
import httpretty
from django.conf import settings
from edx_toggles.toggles.testutils import override_waffle_flag
from openedx_events.learning.signals import COURSE_NOTIFICATION_REQUESTED, USER_NOTIFICATION_REQUESTED

from common.djangoapps.student.models import CourseEnrollment
from common.djangoapps.student.tests.factories import StaffFactory, UserFactory
from lms.djangoapps.discussion.django_comment_client.tests.factories import RoleFactory
from lms.djangoapps.discussion.rest_api.tasks import (
    send_response_endorsed_notifications,
    send_thread_created_notification
)
from lms.djangoapps.discussion.rest_api.tests.utils import ThreadMock, make_minimal_cs_thread
from openedx.core.djangoapps.course_groups.models import CohortMembership, CourseCohortsSettings
from openedx.core.djangoapps.course_groups.tests.helpers import CohortFactory
from openedx.core.djangoapps.discussions.models import DiscussionTopicLink
from openedx.core.djangoapps.django_comment_common.models import (
    FORUM_ROLE_COMMUNITY_TA,
    FORUM_ROLE_GROUP_MODERATOR,
    FORUM_ROLE_MODERATOR,
    FORUM_ROLE_STUDENT,
    CourseDiscussionSettings
)
from openedx.core.djangoapps.notifications.config.waffle import ENABLE_NOTIFICATIONS, ENABLE_NOTIFY_ALL_LEARNERS
from xmodule.modulestore.tests.django_utils import ModuleStoreTestCase
from xmodule.modulestore.tests.factories import CourseFactory

from .test_views import DiscussionAPIViewTestMixin


def _get_mfe_url(course_id, post_id):
    """
    get discussions mfe url to specific post.
    """
    return f"{settings.DISCUSSIONS_MICROFRONTEND_URL}/{str(course_id)}/posts/{post_id}"


@ddt.ddt
@httpretty.activate
@mock.patch.dict("django.conf.settings.FEATURES", {"ENABLE_DISCUSSION_SERVICE": True})
@override_waffle_flag(ENABLE_NOTIFICATIONS, active=True)
class TestNewThreadCreatedNotification(DiscussionAPIViewTestMixin, ModuleStoreTestCase):
    """
    Test cases related to new_discussion_post and new_question_post notification types
    """

    def setUp(self):
        """
        Setup test case
        """
        super().setUp()
        patcher = mock.patch(
            'openedx.core.djangoapps.discussions.config.waffle.ENABLE_FORUM_V2.is_enabled',
            return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Creating a course
        self.course = CourseFactory.create()

        patcher = mock.patch(
            "openedx.core.djangoapps.django_comment_common.comment_client.thread.forum_api.get_course_id_by_thread",
            return_value=self.course.id
        )
        self.mock_get_course_id_by_thread = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "openedx.core.djangoapps.django_comment_common.comment_client.models.forum_api.get_course_id_by_comment",
            return_value=self.course.id
        )
        self.mock_get_course_id_by_comment = patcher.start()
        self.addCleanup(patcher.stop)
        # Creating relative discussion and cohort settings
        CourseCohortsSettings.objects.create(course_id=str(self.course.id))
        CourseDiscussionSettings.objects.create(course_id=str(self.course.id), _divided_discussions='[]')
        self.first_cohort = self.second_cohort = None

        # Duplicating roles
        self.student_role = RoleFactory(name=FORUM_ROLE_STUDENT, course_id=self.course.id)
        self.moderator_role = RoleFactory(name=FORUM_ROLE_MODERATOR, course_id=self.course.id)
        self.ta_role = RoleFactory(name=FORUM_ROLE_COMMUNITY_TA, course_id=self.course.id)
        self.group_community_ta_role = RoleFactory(name=FORUM_ROLE_GROUP_MODERATOR, course_id=self.course.id)

        # Creating users for with roles
        self.author = StaffFactory(course_key=self.course.id, username='Author')
        self.staff = StaffFactory(course_key=self.course.id, username='Staff')

        self.moderator = UserFactory(username='Moderator')
        self.moderator_role.users.add(self.moderator)

        self.ta = UserFactory(username='TA')
        self.ta_role.users.add(self.ta)

        self.group_ta_cohort_1 = UserFactory(username='Group TA 1')
        self.group_ta_cohort_2 = UserFactory(username='Group TA 2')
        self.group_community_ta_role.users.add(self.group_ta_cohort_1)
        self.group_community_ta_role.users.add(self.group_ta_cohort_2)

        self.learner_cohort_1 = UserFactory(username='Learner 1')
        self.learner_cohort_2 = UserFactory(username='Learner 2')
        self.student_role.users.add(self.learner_cohort_1)
        self.student_role.users.add(self.learner_cohort_2)

        # Creating a topic
        self.topic_id = 'test_topic'
        usage_key = self.course.id.make_usage_key('vertical', self.topic_id)
        self.topic = DiscussionTopicLink(
            context_key=self.course.id,
            usage_key=usage_key,
            title=f"Discussion on {self.topic_id}",
            external_id=self.topic_id,
            provider_id="openedx",
            ordering=1,
            enabled_in_context=True,
        )
        self.notification_to_all_users = [
            self.learner_cohort_1, self.learner_cohort_2, self.staff,
            self.moderator, self.ta, self.group_ta_cohort_1, self.group_ta_cohort_2
        ]
        self.privileged_users = [
            self.staff, self.moderator, self.ta
        ]
        self.cohort_1_users = [self.learner_cohort_1, self.group_ta_cohort_1] + self.privileged_users
        self.cohort_2_users = [self.learner_cohort_2, self.group_ta_cohort_2] + self.privileged_users
        self.thread = self._create_thread()

    def _configure_cohorts(self):
        """
        Configure cohort for course and assign membership to users
        """
        course_key_str = str(self.course.id)
        cohort_settings = CourseCohortsSettings.objects.get(course_id=course_key_str)
        cohort_settings.is_cohorted = True
        cohort_settings.save()

        discussion_settings = CourseDiscussionSettings.objects.get(course_id=course_key_str)
        discussion_settings.always_divide_inline_discussions = True
        discussion_settings.save()

        self.first_cohort = CohortFactory(course_id=self.course.id, name="FirstCohort")
        self.second_cohort = CohortFactory(course_id=self.course.id, name="SecondCohort")

        CohortMembership.assign(cohort=self.first_cohort, user=self.learner_cohort_1)
        CohortMembership.assign(cohort=self.first_cohort, user=self.group_ta_cohort_1)
        CohortMembership.assign(cohort=self.second_cohort, user=self.learner_cohort_2)
        CohortMembership.assign(cohort=self.second_cohort, user=self.group_ta_cohort_2)

    def _assign_enrollments(self):
        """
        Enrolls all the user in the course
        """
        user_list = [self.author] + self.notification_to_all_users
        for user in user_list:
            CourseEnrollment.enroll(user, self.course.id)

    def _create_thread(self, thread_type="discussion", group_id=None):
        """
        Create a thread
        """
        thread = make_minimal_cs_thread({
            'id': 1,
            'course_id': str(self.course.id),
            "commentable_id": self.topic_id,
            "username": self.author.username,
            "user_id": str(self.author.id),
            "thread_type": thread_type,
            "group_id": group_id,
            "title": "Test Title",
        })
        self.register_get_thread_response(thread)
        return thread

    def test_basic(self):
        """
        Left empty intentionally. This test case is inherited from DiscussionAPIViewTestMixin
        """

    def test_not_authenticated(self):
        """
        Left empty intentionally. This test case is inherited from DiscussionAPIViewTestMixin
        """

    @ddt.data(
        ('new_question_post', False, False),
        ('new_discussion_post', False, False),
        ('new_discussion_post', True, True),
        ('new_discussion_post', True, False),
    )
    @ddt.unpack
    def test_notification_is_send_to_all_enrollments(
        self, notification_type, notify_all_learners, waffle_flag_enabled
    ):
        """
        Tests notification is sent to all users if course is not cohorted
        """
        self._assign_enrollments()
        thread_type = (
            "discussion" if notification_type == "new_discussion_post" else "question"
        )

        with override_waffle_flag(ENABLE_NOTIFY_ALL_LEARNERS, active=waffle_flag_enabled):
            thread = self._create_thread(thread_type=thread_type)
            handler = mock.Mock()
            COURSE_NOTIFICATION_REQUESTED.connect(handler)

            send_thread_created_notification(
                thread['id'],
                str(self.course.id),
                self.author.id,
                notify_all_learners
            )
            expected_handler_calls = 0 if notify_all_learners and not waffle_flag_enabled else 1
            self.assertEqual(handler.call_count, expected_handler_calls)

            if handler.call_count:
                course_notification_data = handler.call_args[1]['course_notification_data']
                expected_type = (
                    'new_instructor_all_learners_post'
                    if notify_all_learners and waffle_flag_enabled
                    else notification_type
                )
                self.assertEqual(course_notification_data.notification_type, expected_type)
                self.assertEqual(course_notification_data.audience_filters, {})

    @ddt.data(
        ('cohort_1', 'new_question_post'),
        ('cohort_1', 'new_discussion_post'),
        ('cohort_2', 'new_question_post'),
        ('cohort_2', 'new_discussion_post'),
    )
    @ddt.unpack
    def test_notification_is_send_to_cohort_ids(self, cohort_text, notification_type):
        """
        Tests if notification is sent only to privileged users and cohort members if the
        course is cohorted
        """
        self._assign_enrollments()
        self._configure_cohorts()
        cohort, audience = (
            (self.first_cohort, self.cohort_1_users)
            if cohort_text == "cohort_1"
            else ((self.second_cohort, self.cohort_2_users) if cohort_text == "cohort_2" else None)
        )

        thread_type = (
            "discussion"
            if notification_type == "new_discussion_post"
            else ("question" if notification_type == "new_question_post" else "")
        )

        cohort_id = cohort.id
        thread = self._create_thread(group_id=cohort_id, thread_type=thread_type)
        handler = mock.Mock()
        COURSE_NOTIFICATION_REQUESTED.connect(handler)
        send_thread_created_notification(thread['id'], str(self.course.id), self.author.id)
        course_notification_data = handler.call_args[1]['course_notification_data']
        assert notification_type == course_notification_data.notification_type
        notification_audience_filters = {
            'cohorts': [cohort_id],
            'course_roles': ['staff', 'instructor'],
            'discussion_roles': ['Administrator', 'Moderator', 'Community TA'],
        }
        assert notification_audience_filters == handler.call_args[1]['course_notification_data'].audience_filters
        self.assertEqual(handler.call_count, 1)


@override_waffle_flag(ENABLE_NOTIFICATIONS, active=True)
class TestResponseEndorsedNotifications(DiscussionAPIViewTestMixin, ModuleStoreTestCase):
    """
    Test case to send response endorsed notifications
    """

    def setUp(self):
        super().setUp()
        httpretty.reset()
        httpretty.enable()
        patcher = mock.patch(
            'openedx.core.djangoapps.discussions.config.waffle.ENABLE_FORUM_V2.is_enabled',
            return_value=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.course = CourseFactory.create()
        patcher = mock.patch(
            "openedx.core.djangoapps.django_comment_common.comment_client.thread.forum_api.get_course_id_by_thread",
            return_value=self.course.id
        )
        self.mock_get_course_id_by_thread = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch(
            "openedx.core.djangoapps.django_comment_common.comment_client.models.forum_api.get_course_id_by_comment",
            return_value=self.course.id
        )
        self.mock_get_course_id_by_comment = patcher.start()
        self.addCleanup(patcher.stop)
        self.user_1 = UserFactory.create()
        CourseEnrollment.enroll(self.user_1, self.course.id)
        self.user_2 = UserFactory.create()
        self.user_3 = UserFactory.create()
        CourseEnrollment.enroll(self.user_2, self.course.id)
        CourseEnrollment.enroll(self.user_3, self.course.id)

    def test_basic(self):
        """
        Left empty intentionally. This test case is inherited from DiscussionAPIViewTestMixin
        """

    def test_not_authenticated(self):
        """
        Left empty intentionally. This test case is inherited from DiscussionAPIViewTestMixin
        """

    def test_response_endorsed_notifications(self):
        """
        Tests response endorsed notifications
        """
        thread = ThreadMock(thread_id=1, creator=self.user_1, title='test thread')
        response = ThreadMock(thread_id=2, creator=self.user_2, title='test response')
        self.register_get_thread_response({
            'id': thread.id,
            'course_id': str(self.course.id),
            'topic_id': 'abc',
            "user_id": thread.user_id,
            "username": thread.username,
            "thread_type": 'discussion',
            "title": thread.title,
            "commentable_id": thread.commentable_id,
        })
        self.register_get_comment_response({
            'id': 1,
            'thread_id': thread.id,
            'user_id': response.user_id
        })
        self.register_get_comment_response({
            'id': 2,
            'thread_id': thread.id,
            'user_id': response.user_id
        })
        handler = mock.Mock()
        USER_NOTIFICATION_REQUESTED.connect(handler)
        send_response_endorsed_notifications(thread.id, response.id, str(self.course.id), self.user_3.id)
        self.assertEqual(handler.call_count, 2)

        # Test response endorsed on thread notification
        notification_data = handler.call_args_list[0][1]['notification_data']
        # Target only the thread author
        self.assertEqual([int(user_id) for user_id in notification_data.user_ids], [int(thread.user_id)])
        self.assertEqual(notification_data.notification_type, 'response_endorsed_on_thread')

        expected_context = {
            'replier_name': self.user_2.username,
            'post_title': 'test thread',
            'course_name': self.course.display_name,
            'sender_id': int(self.user_2.id),
            'email_content': 'dummy',
            'response_id': None,
            'topic_id': None,
            'thread_id': 1,
            'comment_id': 2,
        }
        self.assertDictEqual(notification_data.context, expected_context)
        self.assertEqual(notification_data.content_url, _get_mfe_url(self.course.id, thread.id))
        self.assertEqual(notification_data.app_name, 'discussion')
        self.assertEqual('response_endorsed_on_thread', notification_data.notification_type)

        # Test response endorsed notification
        notification_data = handler.call_args_list[1][1]['notification_data']
        # Target only the response author
        self.assertEqual([int(user_id) for user_id in notification_data.user_ids], [int(response.user_id)])
        self.assertEqual(notification_data.notification_type, 'response_endorsed')

        expected_context = {
            'replier_name': response.username,
            'post_title': 'test thread',
            'course_name': self.course.display_name,
            'sender_id': int(response.user_id),
            'email_content': 'dummy',
            'response_id': None,
            'topic_id': None,
            'thread_id': 1,
            'comment_id': 2,
        }
        self.assertDictEqual(notification_data.context, expected_context)
        self.assertEqual(notification_data.content_url, _get_mfe_url(self.course.id, thread.id))
        self.assertEqual(notification_data.app_name, 'discussion')
        self.assertEqual('response_endorsed', notification_data.notification_type)
