# Invite new users

There are a number of ways to grant access to your Zulip organization;
the article below describes each in more detail.

* Allow **anyone to join** without an invitation.

* Allow people to join based on the **domain** of their email address.

* Send **email invitations**.

* Share a **reusable invitation link**.

The last two, invite-based, techniques also allow you to control the
[role (owner, admin, moderator, member, or guest)](/help/roles-and-permissions) that the
invited people will have.

You can also manage access by
[controlling how users authenticate](/help/configure-authentication-methods)
to Zulip.  For example, you could allow anyone to join without an
invitation, but require them to authenticate via LDAP.

## Enable email signup

{start_tabs}

{tab|restrict-by-email-domain}

1. Set [default streams](/help/set-default-streams-for-new-users) for new users.

{settings_tab|organization-permissions}

1. Find the section **Joining the organization**.

1. Toggle **Invitations are required for joining this organization**.

1. Set **Restrict email domains of new users?** to
   **Restrict to a list of domains**.

1. Enter any number of domains. For each domain, check or uncheck
   **Allow subdomains**.

1. Click **Save changes**.

{tab|allow-anyone-to-join}

1. Set [default streams](/help/set-default-streams-for-new-users) for new users.

{settings_tab|organization-permissions}

1. Find the section **Joining the organization**.

1. Toggle **Invitations are required for joining this organization**.

1. Set **Restrict email domains of new users?** to either
   **Don't allow disposable email addresses** (recommended) or **No**.

1. Click **Save changes**.

{end_tabs}

Before anyone joins your organization this way, we'll send a validation link
to verify their email address.

## Send invitations

By default, organization admins and members can send
invitations. Organization admins can also change who can send invitations.

Note that on most Zulip servers (including Zulip Cloud), email invitations
and reusable invitation links expire 10 days after they are sent.

{start_tabs}

{tab|send-email-invitations}

{relative|gear|invite}

1. Enter a list of email addresses.

1. Decide whether the users should join as [owners, admins, moderators,
   members, or guests](/help/roles-and-permissions).

1. Select which streams they should join. If you send invitations often, you
   may want to configure a set of
   [default streams](/help/set-default-streams-for-new-users).

1. Click **Invite**.

!!! warn ""

    * You will only see **Invite users** in the gear menu if you have
    permission to invite users.
    * The number of email invites you can send in a day is limited in
    the free plan. [Contact us](/help/contact-support) if you hit the
    limit and want to invite more users.

{tab|share-an-invite-link}

{relative|gear|invite}

1. Click **Generate invite link**.

1. Decide whether users using the link should join as [owners, admins, moderators
   members, or guests](/help/roles-and-permissions).

1. Select which streams they should join. If you send invitations often, you
   may want to configure a set of
   [default streams](/help/set-default-streams-for-new-users).

1. Click **Generate invite link**.

1. Copy the link, and send it to anyone you'd like to invite.

!!! warn ""

    * You will only see **Invite users** in the gear menu if you have
    permission to invite users.
    * Only organization administrators can create these reusable invitation links.


{end_tabs}

## Change who can send invitations

{!owner-only.md!}

By default, all members can invite new users to join your Zulip
organization. However, you can restrict the permission to invite new
users to other sets of roles:

* Nobody
* Organization administrators
* Organization administrators and moderators
* Organization administrators and all members
* Organization administrators and [full members](/help/restrict-permissions-of-new-members)

{start_tabs}

{settings_tab|organization-permissions}

1. Under **Joining the organization**, configure
   **Who can invite users to this organization**.

1. Click **Save changes**.

{end_tabs}

## Manage pending invitations

Organization owners can revoke or resend any invitation or reusable
invitation link. Organization administrators can can do the same
except for invitations for the organization owners role.

{start_tabs}

{settings_tab|invites-list-admin}

1. From here, you can view pending invitations, **Revoke** email invitations
   and invitation links, or **Resend** email invitations.

{end_tabs}

## Related articles

* [Stream permissions](/help/stream-permissions)
* [Roles and permissions](/help/roles-and-permissions)
