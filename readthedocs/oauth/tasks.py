"""Tasks for OAuth services."""

import datetime

import structlog
from allauth.socialaccount.providers import registry as allauth_registry
from django.contrib.auth.models import User
from django.db.models.functions import ExtractIsoWeekDay
from django.urls import reverse
from django.utils import timezone

from readthedocs.core.permissions import AdminPermission
from readthedocs.core.utils.tasks import PublicTask, user_id_matches_or_superuser
from readthedocs.notifications.models import Notification
from readthedocs.oauth.notifications import (
    MESSAGE_OAUTH_WEBHOOK_INVALID,
    MESSAGE_OAUTH_WEBHOOK_NO_ACCOUNT,
    MESSAGE_OAUTH_WEBHOOK_NO_PERMISSIONS,
)
from readthedocs.oauth.services.base import SyncServiceError
from readthedocs.oauth.utils import SERVICE_MAP
from readthedocs.organizations.models import Organization
from readthedocs.projects.models import Project
from readthedocs.sso.models import SSOIntegration
from readthedocs.worker import app

from .services import registry

log = structlog.get_logger(__name__)


@PublicTask.permission_check(user_id_matches_or_superuser)
@app.task(
    queue="web",
    base=PublicTask,
    # We have experienced timeout problems on users having a lot of
    # repositories to sync. This is usually due to users belonging to big
    # organizations (e.g. conda-forge).
    time_limit=900,
    soft_time_limit=600,
)
def sync_remote_repositories(user_id):
    user = User.objects.filter(pk=user_id).first()
    if not user:
        return

    failed_services = set()
    for service_cls in registry:
        for service in service_cls.for_user(user):
            try:
                service.sync()
            except SyncServiceError:
                failed_services.add(service.provider_name)
    if failed_services:
        raise SyncServiceError(
            SyncServiceError.INVALID_OR_REVOKED_ACCESS_TOKEN.format(
                provider=", ".join(failed_services)
            )
        )


@app.task(queue="web")
def sync_remote_repositories_organizations(organization_slugs=None):
    """
    Re-sync users member of organizations.

    It will trigger one `sync_remote_repositories` task per user.

    :param organization_slugs: list containg organization's slugs to sync. If
    not passed, all organizations with ALLAUTH SSO enabled will be synced

    :type organization_slugs: list
    """
    if organization_slugs:
        query = Organization.objects.filter(slug__in=organization_slugs)
        log.info(
            "Triggering SSO re-sync for organizations.",
            organization_slugs=organization_slugs,
            count=query.count(),
        )
    else:
        organization_ids = SSOIntegration.objects.filter(
            provider=SSOIntegration.PROVIDER_ALLAUTH
        ).values_list("organization", flat=True)
        query = Organization.objects.filter(id__in=organization_ids)
        log.info(
            "Triggering SSO re-sync for all organizations.",
            count=query.count(),
        )

    n_task = -1
    for organization in query:
        members = AdminPermission.members(organization)
        log.info(
            "Triggering SSO re-sync for organization.",
            organization_slug=organization.slug,
            count=members.count(),
        )
        for user in members:
            n_task += 1
            sync_remote_repositories.apply_async(
                args=[user.pk],
                # delay the task by 0, 5, 10, 15, ... seconds
                countdown=n_task * 5,
            )


@app.task(
    queue="web",
    time_limit=60 * 60 * 3,  # 3h
    soft_time_limit=(60 * 60 * 3) - 5 * 60,  # 2h 55m
)
def sync_active_users_remote_repositories():
    """
    Sync ``RemoteRepository`` for active users.

    We consider active users those that logged in at least once in the last 90 days.

    This task is thought to be executed daily. It checks the weekday of the
    last login of the user with today's weekday. If they match, the re-sync is
    triggered. This logic guarantees us the re-sync to be done once a week per user.

    Note this is a long running task syncronizhing all the users in the same Celery process,
    and it will require a pretty high ``time_limit`` and ``soft_time_limit``.
    """
    today_weekday = timezone.now().isoweekday()
    three_months_ago = timezone.now() - datetime.timedelta(days=90)
    users = User.objects.annotate(weekday=ExtractIsoWeekDay("last_login")).filter(
        last_login__gt=three_months_ago,
        socialaccount__isnull=False,
        weekday=today_weekday,
    )

    users_count = users.count()
    log.bind(total_users=users_count)
    log.info("Triggering re-sync of RemoteRepository for active users.")

    for i, user in enumerate(users):
        log.bind(
            user_username=user.username,
            progress=f"{i}/{users_count}",
        )

        # Log an update every 50 users
        if i % 50 == 0:
            log.info("Progress on re-syncing RemoteRepository for active users.")

        try:
            # NOTE: sync all the users/repositories in the same Celery process.
            # Do not trigger a new task per user.
            sync_remote_repositories(user.pk)
        except Exception:
            log.exception("There was a problem re-syncing RemoteRepository.")


@app.task(queue="web")
def attach_webhook(project_pk, user_pk, integration=None):
    """
    Add post-commit hook on project import.

    This is a brute force approach to add a webhook to a repository. We try
    all accounts until we set up a webhook. This should remain around for legacy
    connections -- that is, projects that do not have a remote repository them
    and were not set up with a VCS provider.
    """
    project = Project.objects.filter(pk=project_pk).first()
    user = User.objects.filter(pk=user_pk).first()

    if not project or not user:
        return False

    if integration:
        service = SERVICE_MAP.get(integration.integration_type)

        if not service:
            log.warning("There are no registered services in the application.")
            Notification.objects.add(
                message_id=MESSAGE_OAUTH_WEBHOOK_INVALID,
                attached_to=project,
                dismissable=True,
                format_values={
                    "url_integrations": reverse(
                        "projects_integrations",
                        args=[project.slug],
                    ),
                },
            )
            return None
    else:
        for service_cls in registry:
            if service_cls.is_project_service(project):
                service = service_cls
                break
        else:
            log.warning("There are no registered services in the application.")
            Notification.objects.add(
                message_id=MESSAGE_OAUTH_WEBHOOK_INVALID,
                attached_to=project,
                dismissable=True,
                format_values={
                    "url_integrations": reverse(
                        "projects_integrations",
                        args=[project.slug],
                    ),
                },
            )
            return None

    provider = allauth_registry.by_id(service.adapter.provider_id)

    user_accounts = service.for_user(user)
    for account in user_accounts:
        success, __ = account.setup_webhook(project, integration=integration)
        if success:
            # NOTE: do we want to communicate that we connect the webhook here?
            # messages.add_message(request, "Webhook successfully added.")

            project.has_valid_webhook = True
            project.save()
            return True

    # No valid account found
    if user_accounts:
        Notification.objects.add(
            message_id=MESSAGE_OAUTH_WEBHOOK_NO_PERMISSIONS,
            dismissable=True,
            attached_to=project,
            format_values={
                "provider_name": provider.name,
                "url_docs_webhook": "https://docs.readthedocs.io/page/webhooks.html",
            },
        )
    else:
        Notification.objects.add(
            message_id=MESSAGE_OAUTH_WEBHOOK_NO_ACCOUNT,
            dismissable=True,
            attached_to=project,
            format_values={
                "provider_name": provider.name,
                "url_connect_account": reverse(
                    "projects_integrations",
                    args=[project.slug],
                ),
            },
        )

    return False
