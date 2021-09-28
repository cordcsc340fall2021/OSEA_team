from typing import Any, Dict, List

from django.db import transaction
from django.utils.translation import gettext as _

from zerver.lib.exceptions import JsonableError
from zerver.models import Realm, UserGroup, UserGroupMembership, UserProfile


def access_user_group_by_id(
    user_group_id: int, user_profile: UserProfile, for_mention: bool = False
) -> UserGroup:
    try:
        user_group = UserGroup.objects.get(id=user_group_id, realm=user_profile.realm)
        if not for_mention and user_group.is_system_group:
            raise JsonableError(_("Insufficient permission"))
        group_member_ids = get_user_group_members(user_group)
        if (
            not user_profile.is_realm_admin
            and not user_profile.is_moderator
            and user_profile.id not in group_member_ids
        ):
            raise JsonableError(_("Insufficient permission"))
    except UserGroup.DoesNotExist:
        raise JsonableError(_("Invalid user group"))
    return user_group


def user_groups_in_realm_serialized(realm: Realm) -> List[Dict[str, Any]]:
    """This function is used in do_events_register code path so this code
    should be performant.  We need to do 2 database queries because
    Django's ORM doesn't properly support the left join between
    UserGroup and UserGroupMembership that we need.
    """
    realm_groups = UserGroup.objects.filter(realm=realm)
    group_dicts: Dict[str, Any] = {}
    for user_group in realm_groups:
        group_dicts[user_group.id] = dict(
            id=user_group.id,
            name=user_group.name,
            description=user_group.description,
            members=[],
            is_system_group=user_group.is_system_group,
        )

    membership = UserGroupMembership.objects.filter(user_group__realm=realm).values_list(
        "user_group_id", "user_profile_id"
    )
    for (user_group_id, user_profile_id) in membership:
        group_dicts[user_group_id]["members"].append(user_profile_id)
    for group_dict in group_dicts.values():
        group_dict["members"] = sorted(group_dict["members"])

    return sorted(group_dicts.values(), key=lambda group_dict: group_dict["id"])


def get_user_groups(user_profile: UserProfile) -> List[UserGroup]:
    return list(user_profile.usergroup_set.all())


def remove_user_from_user_group(user_profile: UserProfile, user_group: UserGroup) -> int:
    num_deleted, _ = UserGroupMembership.objects.filter(
        user_profile=user_profile, user_group=user_group
    ).delete()
    return num_deleted


def create_user_group(
    name: str,
    members: List[UserProfile],
    realm: Realm,
    *,
    description: str = "",
    is_system_group: bool = False,
) -> UserGroup:
    with transaction.atomic():
        user_group = UserGroup.objects.create(
            name=name, realm=realm, description=description, is_system_group=is_system_group
        )
        UserGroupMembership.objects.bulk_create(
            UserGroupMembership(user_profile=member, user_group=user_group) for member in members
        )
        return user_group


def get_user_group_members(user_group: UserGroup) -> List[UserProfile]:
    members = UserGroupMembership.objects.filter(user_group=user_group)
    return [member.user_profile.id for member in members]


def get_memberships_of_users(user_group: UserGroup, members: List[UserProfile]) -> List[int]:
    return list(
        UserGroupMembership.objects.filter(
            user_group=user_group, user_profile__in=members
        ).values_list("user_profile_id", flat=True)
    )
