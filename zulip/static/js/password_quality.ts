import zxcvbn from "zxcvbn";

import {$t} from "./i18n";

// Note: this module is loaded asynchronously from the app with
// import() to keep zxcvbn out of the initial page load.  Do not
// import it synchronously from the app.

// Return a boolean indicating whether the password is acceptable.
// Also updates a Bootstrap progress bar control (a jQuery object)
// if provided.
export function password_quality(
    password: string,
    bar: JQuery | undefined,
    password_field: JQuery,
): boolean {
    const min_length = password_field.data("minLength");
    const min_guesses = password_field.data("minGuesses");

    const result = zxcvbn(password);
    const acceptable = password.length >= min_length && result.guesses >= min_guesses;

    if (bar !== undefined) {
        const t = Number(result.crack_times_seconds.offline_slow_hashing_1e4_per_second);
        let bar_progress = Math.min(1, Math.log(1 + t) / 22);

        // Even if zxcvbn loves your short password, the bar should be
        // filled at most 1/3 of the way, because we won't accept it.
        if (!acceptable) {
            bar_progress = Math.min(bar_progress, 0.33);
        }

        // The bar bottoms out at 10% so there's always something
        // for the user to see.
        bar.width(`${90 * bar_progress + 10}%`)
            .removeClass("bar-success bar-danger")
            .addClass(acceptable ? "bar-success" : "bar-danger");
    }

    return acceptable;
}

export function password_warning(password: string, password_field: JQuery): string {
    const min_length = password_field.data("minLength");

    if (password.length < min_length) {
        return $t(
            {defaultMessage: "Password should be at least {length} characters long"},
            {length: min_length},
        );
    }
    return zxcvbn(password).feedback.warning || $t({defaultMessage: "Password is too weak"});
}
