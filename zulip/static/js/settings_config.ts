import Handlebars from "handlebars/runtime";

import {$t, $t_html} from "./i18n";
import {page_params} from "./page_params";
import type {RealmDefaultSettingsType} from "./realm_user_settings_defaults";
import type {UserSettingsType} from "./user_settings";

/*
    This file contains translations between the integer values used in
    the Zulip API to describe values in dropdowns, radio buttons, and
    similar widgets and the user-facing strings that should be used to
    describe them, as well as data details like sort orders that may
    be useful for some widgets.

    We plan to eventually transition much of this file to have a more
    standard format and then to be populated using data sent from the
    Zulip server in `page_params`, so that the data is available for
    other parts of the ecosystem to use (including the mobile apps and
    API documentation) without a ton of copying.
*/

export const demote_inactive_streams_values = {
    automatic: {
        code: 1,
        description: $t({defaultMessage: "Automatic"}),
    },
    always: {
        code: 2,
        description: $t({defaultMessage: "Always"}),
    },
    never: {
        code: 3,
        description: $t({defaultMessage: "Never"}),
    },
};

export const default_view_values = {
    recent_topics: {
        code: "recent_topics",
        description: $t({defaultMessage: "Recent topics"}),
    },
    all_messages: {
        code: "all_messages",
        description: $t({defaultMessage: "All messages"}),
    },
};

export const color_scheme_values = {
    automatic: {
        code: 1,
        description: $t({defaultMessage: "Automatic"}),
    },
    night: {
        code: 2,
        description: $t({defaultMessage: "Night mode"}),
    },
    day: {
        code: 3,
        description: $t({defaultMessage: "Day mode"}),
    },
};

export const twenty_four_hour_time_values = {
    twenty_four_hour_clock: {
        value: true,
        description: $t({defaultMessage: "24-hour clock (17:00)"}),
    },
    twelve_hour_clock: {
        value: false,
        description: $t({defaultMessage: "12-hour clock (5:00 PM)"}),
    },
};

export interface DisplaySettings {
    settings: {
        user_display_settings: string[];
    };
    render_only: {
        high_contrast_mode: boolean;
        dense_mode: boolean;
    };
}

export const get_all_display_settings = (): DisplaySettings => ({
    settings: {
        user_display_settings: [
            "dense_mode",
            "high_contrast_mode",
            "left_side_userlist",
            "starred_message_counts",
            "fluid_layout_width",
        ],
    },
    render_only: {
        high_contrast_mode: page_params.development_environment,
        dense_mode: page_params.development_environment,
    },
});

export const email_address_visibility_values = {
    everyone: {
        code: 1,
        description: $t({defaultMessage: "Admins, moderators, members and guests"}),
    },
    // // Backend support for this configuration is not available yet.
    // admins_and_members: {
    //     code: 2,
    //     description: $t({defaultMessage: "Members and admins"}),
    // },
    moderators: {
        code: 5,
        description: $t({defaultMessage: "Admins and moderators"}),
    },
    admins_only: {
        code: 3,
        description: $t({defaultMessage: "Admins only"}),
    },
    nobody: {
        code: 4,
        description: $t({defaultMessage: "Nobody"}),
    },
};

export const common_policy_values = {
    by_admins_only: {
        order: 1,
        code: 2,
        description: $t({defaultMessage: "Admins"}),
    },
    by_moderators_only: {
        order: 2,
        code: 4,
        description: $t({defaultMessage: "Admins and moderators"}),
    },
    by_full_members: {
        order: 3,
        code: 3,
        description: $t({defaultMessage: "Admins, moderators and full members"}),
    },
    by_members: {
        order: 4,
        code: 1,
        description: $t({defaultMessage: "Admins, moderators and members"}),
    },
};

export const invite_to_realm_policy_values = {
    nobody: {
        order: 1,
        code: 6,
        description: $t({defaultMessage: "Nobody"}),
    },
    by_admins_only: {
        order: 2,
        code: 2,
        description: $t({defaultMessage: "Admins"}),
    },
    by_moderators_only: {
        order: 3,
        code: 4,
        description: $t({defaultMessage: "Admins and moderators"}),
    },
    by_full_members: {
        order: 4,
        code: 3,
        description: $t({defaultMessage: "Admins, moderators and full members"}),
    },
    by_members: {
        order: 5,
        code: 1,
        description: $t({defaultMessage: "Admins, moderators and members"}),
    },
};

export const private_message_policy_values = {
    by_anyone: {
        order: 1,
        code: 1,
        description: $t({defaultMessage: "Admins, moderators, members and guests"}),
    },
    disabled: {
        order: 2,
        code: 2,
        description: $t({defaultMessage: "Private messages disabled"}),
    },
};

export const wildcard_mention_policy_values = {
    by_everyone: {
        order: 1,
        code: 1,
        description: $t({defaultMessage: "Admins, moderators, members and guests"}),
    },
    by_members: {
        order: 2,
        code: 2,
        description: $t({defaultMessage: "Admins, moderators and members"}),
    },
    by_full_members: {
        order: 3,
        code: 3,
        description: $t({defaultMessage: "Admins, moderators and full members"}),
    },
    by_moderators_only: {
        order: 4,
        code: 7,
        description: $t({defaultMessage: "Admins and moderators"}),
    },
    // Until we add stream administrators, we mislabel this choice
    // (which we intend to be the long-term default) as "Admins only"
    // and don't offer the long-term "Admins only" option.
    by_stream_admins_only: {
        order: 5,
        code: 4,
        //  description: $t({defaultMessage: "Organization and stream admins"}),
        description: $t({defaultMessage: "Admins only"}),
    },
    // by_admins_only: {
    //     order: 5,
    //     code: 5,
    //     description: $t({defaultMessage: "Admins only"}),
    // },
    nobody: {
        order: 6,
        code: 6,
        description: $t({defaultMessage: "Nobody"}),
    },
};

export const common_message_policy_values = {
    by_everyone: {
        order: 1,
        code: 5,
        description: $t({defaultMessage: "Admins, moderators, members and guests"}),
    },
    by_members: {
        order: 2,
        code: 1,
        description: $t({defaultMessage: "Admins, moderators and members"}),
    },
    by_full_members: {
        order: 3,
        code: 3,
        description: $t({defaultMessage: "Admins, moderators and full members"}),
    },
    by_moderators_only: {
        order: 4,
        code: 4,
        description: $t({defaultMessage: "Admins and moderators"}),
    },
    by_admins_only: {
        order: 5,
        code: 2,
        description: $t({defaultMessage: "Admins only"}),
    },
};

const time_limit_dropdown_values = new Map([
    [
        "any_time",
        {
            text: $t({defaultMessage: "Any time"}),
            seconds: 0,
        },
    ],
    [
        "never",
        {
            text: $t({defaultMessage: "Never"}),
        },
    ],
    [
        "upto_two_min",
        {
            text: $t(
                {defaultMessage: "Up to {time_limit} after posting"},
                {time_limit: $t({defaultMessage: "2 minutes"})},
            ),
            seconds: 2 * 60,
        },
    ],
    [
        "upto_ten_min",
        {
            text: $t(
                {defaultMessage: "Up to {time_limit} after posting"},
                {time_limit: $t({defaultMessage: "10 minutes"})},
            ),
            seconds: 10 * 60,
        },
    ],
    [
        "upto_one_hour",
        {
            text: $t(
                {defaultMessage: "Up to {time_limit} after posting"},
                {time_limit: $t({defaultMessage: "1 hour"})},
            ),
            seconds: 60 * 60,
        },
    ],
    [
        "upto_one_day",
        {
            text: $t(
                {defaultMessage: "Up to {time_limit} after posting"},
                {time_limit: $t({defaultMessage: "1 day"})},
            ),
            seconds: 24 * 60 * 60,
        },
    ],
    [
        "upto_one_week",
        {
            text: $t(
                {defaultMessage: "Up to {time_limit} after posting"},
                {time_limit: $t({defaultMessage: "1 week"})},
            ),
            seconds: 7 * 24 * 60 * 60,
        },
    ],
    [
        "custom_limit",
        {
            text: $t({defaultMessage: "Up to N minutes after posting"}),
        },
    ],
]);
export const msg_edit_limit_dropdown_values = time_limit_dropdown_values;
export const msg_delete_limit_dropdown_values = time_limit_dropdown_values;
export const retain_message_forever = -1;

export const user_role_values = {
    guest: {
        code: 600,
        description: $t({defaultMessage: "Guest"}),
    },
    member: {
        code: 400,
        description: $t({defaultMessage: "Member"}),
    },
    moderator: {
        code: 300,
        description: $t({defaultMessage: "Moderator"}),
    },
    admin: {
        code: 200,
        description: $t({defaultMessage: "Administrator"}),
    },
    owner: {
        code: 100,
        description: $t({defaultMessage: "Owner"}),
    },
};

const user_role_array = Object.values(user_role_values);
export const user_role_map = new Map(user_role_array.map((role) => [role.code, role.description]));

export const display_settings_labels = {
    dense_mode: $t({defaultMessage: "Dense mode"}),
    fluid_layout_width: $t({defaultMessage: "Use full width on wide screens"}),
    high_contrast_mode: $t({defaultMessage: "High contrast mode"}),
    left_side_userlist: $t({
        defaultMessage: "Show user list on left sidebar in narrow windows",
    }),
    starred_message_counts: $t({defaultMessage: "Show counts for starred messages"}),
    twenty_four_hour_time: $t({defaultMessage: "Time format"}),
    translate_emoticons: new Handlebars.SafeString(
        $t_html({
            defaultMessage: "Convert emoticons before sending (<code>:)</code> becomes 😃)",
        }),
    ),
};

export const notification_settings_labels = {
    enable_online_push_notifications: $t({
        defaultMessage: "Send mobile notifications even if I'm online (useful for testing)",
    }),
    pm_content_in_desktop_notifications: $t({
        defaultMessage: "Include content of private messages in desktop notifications",
    }),
    desktop_icon_count_display: $t({
        defaultMessage: "Unread count summary (appears in desktop sidebar and browser tab)",
    }),
    enable_digest_emails: $t({defaultMessage: "Send digest emails when I'm away"}),
    enable_login_emails: $t({
        defaultMessage: "Send email notifications for new logins to my account",
    }),
    enable_marketing_emails: $t({
        defaultMessage: "Send me Zulip's low-traffic newsletter (a few emails a year)",
    }),
    message_content_in_email_notifications: $t({
        defaultMessage: "Include message content in message notification emails",
    }),
    realm_name_in_notifications: $t({
        defaultMessage: "Include organization name in subject of message notification emails",
    }),
};

export const realm_user_settings_defaults_labels = {
    ...notification_settings_labels,
    ...display_settings_labels,

    /* Overrides to remove "I" from labels for the realm-level versions of these labels. */
    enable_online_push_notifications: $t({
        defaultMessage: "Send mobile notifications even if user is online (useful for testing)",
    }),
    enable_digest_emails: $t({defaultMessage: "Send digest emails when user is away"}),
    enable_login_emails: $t({
        defaultMessage: "Send email notifications for new logins to the account",
    }),

    realm_presence_enabled: $t({defaultMessage: "Display availability to other users when online"}),
    realm_enter_sends: $t({defaultMessage: "Enter sends when composing a message"}),
};

// NOTIFICATIONS

export const general_notifications_table_labels = {
    realm: [
        /* An array of notification settings of any category like
         * `stream_notification_settings` which makes a single row of
         * "Notification triggers" table should follow this order
         */
        "visual",
        "audio",
        "mobile",
        "email",
        "all_mentions",
    ],
    stream: {
        is_muted: $t({defaultMessage: "Mute stream"}),
        desktop_notifications: $t({defaultMessage: "Visual desktop notifications"}),
        audible_notifications: $t({defaultMessage: "Audible desktop notifications"}),
        push_notifications: $t({defaultMessage: "Mobile notifications"}),
        email_notifications: $t({defaultMessage: "Email notifications"}),
        pin_to_top: $t({defaultMessage: "Pin stream to top of left sidebar"}),
        wildcard_mentions_notify: $t({defaultMessage: "Notifications for @all/@everyone mentions"}),
    },
};

export const stream_specific_notification_settings = [
    "desktop_notifications",
    "audible_notifications",
    "push_notifications",
    "email_notifications",
    "wildcard_mentions_notify",
];

type SettingsObjectType = UserSettingsType | RealmDefaultSettingsType;
type PageParamsItem = keyof SettingsObjectType;
export const stream_notification_settings: PageParamsItem[] = [
    "enable_stream_desktop_notifications",
    "enable_stream_audible_notifications",
    "enable_stream_push_notifications",
    "enable_stream_email_notifications",
    "wildcard_mentions_notify",
];

export const pm_mention_notification_settings: PageParamsItem[] = [
    "enable_desktop_notifications",
    "enable_sounds",
    "enable_offline_push_notifications",
    "enable_offline_email_notifications",
];

const desktop_notification_settings = ["pm_content_in_desktop_notifications"];

const mobile_notification_settings = ["enable_online_push_notifications"];

export const email_notifications_batching_period_values = [
    {
        value: 60 * 2,
        description: $t({defaultMessage: "2 minutes"}),
    },
    {
        value: 60 * 5,
        description: $t({defaultMessage: "5 minutes"}),
    },
    {
        value: 60 * 10,
        description: $t({defaultMessage: "10 minutes"}),
    },
    {
        value: 60 * 30,
        description: $t({defaultMessage: "30 minutes"}),
    },
    {
        value: 60 * 60,
        description: $t({defaultMessage: "1 hour"}),
    },
    {
        value: 60 * 60 * 6,
        description: $t({defaultMessage: "6 hours"}),
    },
    {
        value: 60 * 60 * 24,
        description: $t({defaultMessage: "1 day"}),
    },
    {
        value: 60 * 60 * 24 * 7,
        description: $t({defaultMessage: "1 week"}),
    },
];

const email_message_notification_settings = [
    "message_content_in_email_notifications",
    "realm_name_in_notifications",
];

const other_email_settings = [
    "enable_digest_emails",
    "enable_login_emails",
    "enable_marketing_emails",
];

const email_notification_settings = other_email_settings.concat(
    email_message_notification_settings,
);

const other_notification_settings = desktop_notification_settings.concat(
    ["desktop_icon_count_display"],
    mobile_notification_settings,
    email_notification_settings,
    ["email_notifications_batching_period_seconds"],
    ["notification_sound"],
);

export const all_notification_settings = other_notification_settings.concat(
    pm_mention_notification_settings,
    stream_notification_settings,
);

type NotificationSettingCheckbox = {
    setting_name: string;
    is_disabled: boolean;
    is_checked: boolean;
};

export function get_notifications_table_row_data(
    notify_settings: PageParamsItem[],
    settings_object: SettingsObjectType,
): NotificationSettingCheckbox[] {
    return general_notifications_table_labels.realm.map((column, index) => {
        const setting_name = notify_settings[index];
        if (setting_name === undefined) {
            return {
                setting_name: "",
                is_disabled: true,
                is_checked: false,
            };
        }

        const checked = settings_object[setting_name];
        if (typeof checked !== "boolean") {
            throw new TypeError(`Incorrect setting_name passed: ${setting_name}`);
        }

        const checkbox = {
            setting_name,
            is_disabled: false,
            is_checked: checked,
        };
        if (column === "mobile") {
            checkbox.is_disabled = !page_params.realm_push_notifications_enabled;
        }
        return checkbox;
    });
}

export interface AllNotifications {
    general_settings: {label: string; notification_settings: NotificationSettingCheckbox[]}[];
    settings: {
        desktop_notification_settings: string[];
        mobile_notification_settings: string[];
        email_message_notification_settings: string[];
        other_email_settings: string[];
    };
    show_push_notifications_tooltip: {
        push_notifications: boolean;
        enable_online_push_notifications: boolean;
    };
}

export const all_notifications = (settings_object: SettingsObjectType): AllNotifications => ({
    general_settings: [
        {
            label: $t({defaultMessage: "Streams"}),
            notification_settings: get_notifications_table_row_data(
                stream_notification_settings,
                settings_object,
            ),
        },
        {
            label: $t({defaultMessage: "PMs, mentions, and alerts"}),
            notification_settings: get_notifications_table_row_data(
                pm_mention_notification_settings,
                settings_object,
            ),
        },
    ],
    settings: {
        desktop_notification_settings,
        mobile_notification_settings,
        email_message_notification_settings,
        other_email_settings,
    },
    show_push_notifications_tooltip: {
        push_notifications: !page_params.realm_push_notifications_enabled,
        enable_online_push_notifications: !page_params.realm_push_notifications_enabled,
    },
});

export const desktop_icon_count_display_values = {
    messages: {
        code: 1,
        description: $t({defaultMessage: "All unreads"}),
    },
    notifiable: {
        code: 2,
        description: $t({defaultMessage: "Private messages and mentions"}),
    },
    none: {
        code: 3,
        description: $t({defaultMessage: "None"}),
    },
};
