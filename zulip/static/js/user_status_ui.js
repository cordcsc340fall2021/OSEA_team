import $ from "jquery";

import * as emoji from "../shared/js/emoji";
import render_set_status_overlay from "../templates/set_status_overlay.hbs";
import render_status_emoji_selector from "../templates/status_emoji_selector.hbs";

import {$t} from "./i18n";
import * as overlays from "./overlays";
import * as people from "./people";
import * as user_status from "./user_status";

let selected_emoji_info = {};
let default_status_messages_and_emoji_info;

export function set_selected_emoji_info(emoji_info) {
    selected_emoji_info = {...emoji_info};
    rebuild_status_emoji_selector_ui(selected_emoji_info);
}
export function input_field() {
    return $("#set_user_status_modal input.user_status");
}

export function submit_button() {
    return $("#set_user_status_modal .set_user_status");
}

export function open_user_status_modal() {
    const user_id = people.my_current_user_id();
    const selected_emoji_info = user_status.get_status_emoji(user_id) || {};
    const rendered_set_status_overlay = render_set_status_overlay({
        default_status_messages_and_emoji_info,
        selected_emoji_info,
    });
    $(".app").append(rendered_set_status_overlay);

    overlays.open_modal("#set_user_status_modal", {autoremove: true});

    const old_status_text = user_status.get_status_text(user_id);
    const old_emoji_info = user_status.get_status_emoji(user_id) || {};
    set_selected_emoji_info(old_emoji_info);
    const field = input_field();
    field.val(old_status_text);
    field.trigger("select");
    toggle_clear_message_button();

    const button = submit_button();
    button.prop("disabled", true);
}

export function close_user_status_modal() {
    overlays.close_modal("#set_user_status_modal");
}

export function submit_new_status() {
    const user_id = people.my_current_user_id();
    let old_status_text = user_status.get_status_text(user_id) || "";
    old_status_text = old_status_text.trim();
    const old_emoji_info = user_status.get_status_emoji(user_id) || {};
    const new_status_text = input_field().val().trim();

    if (
        old_status_text === new_status_text &&
        old_emoji_info.emoji_name === selected_emoji_info.emoji_name &&
        old_emoji_info.reaction_type === selected_emoji_info.reaction_type &&
        old_emoji_info.emoji_code === selected_emoji_info.emoji_code
    ) {
        close_user_status_modal();
        return;
    }

    user_status.server_update({
        status_text: new_status_text,
        emoji_name: selected_emoji_info.emoji_name || "",
        emoji_code: selected_emoji_info.emoji_code || "",
        reaction_type: selected_emoji_info.reaction_type || "",
        success() {
            close_user_status_modal();
        },
    });
}

export function update_button() {
    const user_id = people.my_current_user_id();
    let old_status_text = user_status.get_status_text(user_id) || "";
    old_status_text = old_status_text.trim();
    const old_emoji_info = user_status.get_status_emoji(user_id) || {};
    const new_status_text = input_field().val().trim();
    const button = submit_button();

    if (
        old_status_text === new_status_text &&
        old_emoji_info.emoji_name === selected_emoji_info.emoji_name &&
        old_emoji_info.reaction_type === selected_emoji_info.reaction_type &&
        old_emoji_info.emoji_code === selected_emoji_info.emoji_code
    ) {
        button.prop("disabled", true);
    } else {
        button.prop("disabled", false);
    }
}

export function toggle_clear_message_button() {
    if (input_field().val() !== "" || selected_emoji_info.emoji_name) {
        $("#clear_status_message_button").prop("disabled", false);
    } else {
        $("#clear_status_message_button").prop("disabled", true);
    }
}

export function clear_message() {
    const field = input_field();
    field.val("");
    $("#clear_status_message_button").prop("disabled", true);
}

export function user_status_picker_open() {
    return $("#set_user_status_modal").length !== 0;
}

function rebuild_status_emoji_selector_ui(selected_emoji_info) {
    let selected_emoji = null;
    if (selected_emoji_info && Object.keys(selected_emoji_info).length) {
        selected_emoji = selected_emoji_info;
    }
    const rendered_status_emoji_selector = render_status_emoji_selector({selected_emoji});
    $("#set_user_status_modal .status_emoji_wrapper").html(rendered_status_emoji_selector);
}

export function initialize() {
    default_status_messages_and_emoji_info = [
        {
            status_text: $t({defaultMessage: "In a meeting"}),
            emoji: emoji.get_emoji_details_by_name("calendar"),
        },
        {
            status_text: $t({defaultMessage: "Commuting"}),
            emoji: emoji.get_emoji_details_by_name("bus"),
        },
        {
            status_text: $t({defaultMessage: "Out sick"}),
            emoji: emoji.get_emoji_details_by_name("hurt"),
        },
        {
            status_text: $t({defaultMessage: "Vacationing"}),
            emoji: emoji.get_emoji_details_by_name("palm_tree"),
        },
        {
            status_text: $t({defaultMessage: "Working remotely"}),
            emoji: emoji.get_emoji_details_by_name("house"),
        },
    ];
    $("body").on("click", ".user-status-value", (event) => {
        event.stopPropagation();
        const user_status_value = $(event.currentTarget).text().trim();
        $("input.user_status").val(user_status_value);

        const emoji_info = default_status_messages_and_emoji_info.find(
            (status) => status.status_text === user_status_value,
        ).emoji;
        set_selected_emoji_info(emoji_info);
        toggle_clear_message_button();
        update_button();
    });

    $("body").on("click", "#set_user_status_modal .set_user_status", () => {
        submit_new_status();
    });

    $("body").on("shown.bs.modal", "#set_user_status_modal", () => {
        const field = input_field();
        field.trigger("focus");
    });

    $("body").on("keypress", "#set_user_status_modal .user_status", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();

            submit_new_status();
        }
    });

    $("body").on("keyup", "#set_user_status_modal input.user_status", () => {
        update_button();
        toggle_clear_message_button();
    });

    $("body").on("click", "#clear_status_message_button", () => {
        clear_message();
        set_selected_emoji_info();
        update_button();
    });
}
