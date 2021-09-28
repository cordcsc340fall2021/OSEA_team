import json
import operator
import os
import random
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
)
from unittest.mock import Mock, patch

import orjson
import responses
import stripe
from django.conf import settings
from django.core import signing
from django.http import HttpResponse
from django.urls.resolvers import get_resolver
from django.utils.timezone import now as timezone_now

from corporate.lib.stripe import (
    DEFAULT_INVOICE_DAYS_UNTIL_DUE,
    MAX_INVOICED_LICENSES,
    MIN_INVOICED_LICENSES,
    BillingError,
    InvalidBillingSchedule,
    StripeCardError,
    add_months,
    approve_sponsorship,
    attach_discount_to_realm,
    catch_stripe_errors,
    compute_plan_parameters,
    customer_has_credit_card_as_default_source,
    do_create_stripe_customer,
    downgrade_small_realms_behind_on_payments_as_needed,
    get_discount_for_realm,
    get_latest_seat_count,
    get_price_per_license,
    get_realms_to_default_discount_dict,
    invoice_plan,
    invoice_plans_as_needed,
    is_realm_on_free_trial,
    is_sponsored_realm,
    make_end_of_cycle_updates_if_needed,
    next_month,
    process_initial_upgrade,
    sign_string,
    stripe_customer_has_credit_card_as_default_source,
    stripe_get_customer,
    unsign_string,
    update_billing_method_of_current_plan,
    update_license_ledger_for_automanaged_plan,
    update_license_ledger_for_manual_plan,
    update_license_ledger_if_needed,
    update_or_create_stripe_customer,
    update_sponsorship_status,
    void_all_open_invoices,
)
from corporate.models import (
    Customer,
    CustomerPlan,
    LicenseLedger,
    ZulipSponsorshipRequest,
    get_current_plan_by_customer,
    get_current_plan_by_realm,
    get_customer_by_realm,
)
from zerver.lib.actions import (
    do_activate_mirror_dummy_user,
    do_create_realm,
    do_create_user,
    do_deactivate_realm,
    do_deactivate_user,
    do_reactivate_realm,
    do_reactivate_user,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.timestamp import datetime_to_timestamp, timestamp_to_datetime
from zerver.lib.utils import assert_is_not_none
from zerver.models import (
    Message,
    Realm,
    RealmAuditLog,
    Recipient,
    UserProfile,
    get_realm,
    get_system_bot,
)

CallableT = TypeVar("CallableT", bound=Callable[..., Any])

STRIPE_FIXTURES_DIR = "corporate/tests/stripe_fixtures"

# TODO: check that this creates a token similar to what is created by our
# actual Stripe Checkout flows
def stripe_create_token(card_number: str = "4242424242424242") -> stripe.Token:
    return stripe.Token.create(
        card={
            "number": card_number,
            "exp_month": 3,
            "exp_year": 2033,
            "cvc": "333",
            "name": "Ada Starr",
            "address_line1": "Under the sea,",
            "address_city": "Pacific",
            "address_zip": "33333",
            "address_country": "United States",
        }
    )


def stripe_fixture_path(
    decorated_function_name: str, mocked_function_name: str, call_count: int
) -> str:
    # Make the eventual filename a bit shorter, and also we conventionally
    # use test_* for the python test files
    if decorated_function_name[:5] == "test_":
        decorated_function_name = decorated_function_name[5:]
    return f"{STRIPE_FIXTURES_DIR}/{decorated_function_name}--{mocked_function_name[7:]}.{call_count}.json"


def fixture_files_for_function(decorated_function: CallableT) -> List[str]:  # nocoverage
    decorated_function_name = decorated_function.__name__
    if decorated_function_name[:5] == "test_":
        decorated_function_name = decorated_function_name[5:]
    return sorted(
        f"{STRIPE_FIXTURES_DIR}/{f}"
        for f in os.listdir(STRIPE_FIXTURES_DIR)
        if f.startswith(decorated_function_name + "--")
    )


def generate_and_save_stripe_fixture(
    decorated_function_name: str, mocked_function_name: str, mocked_function: CallableT
) -> Callable[[Any, Any], Any]:  # nocoverage
    def _generate_and_save_stripe_fixture(*args: Any, **kwargs: Any) -> Any:
        # Note that mock is not the same as mocked_function, even though their
        # definitions look the same
        mock = operator.attrgetter(mocked_function_name)(sys.modules[__name__])
        fixture_path = stripe_fixture_path(
            decorated_function_name, mocked_function_name, mock.call_count
        )
        try:
            with responses.RequestsMock() as request_mock:
                request_mock.add_passthru("https://api.stripe.com")
                # Talk to Stripe
                stripe_object = mocked_function(*args, **kwargs)
        except stripe.error.StripeError as e:
            with open(fixture_path, "w") as f:
                error_dict = e.__dict__
                error_dict["headers"] = dict(error_dict["headers"])
                f.write(
                    json.dumps(error_dict, indent=2, separators=(",", ": "), sort_keys=True) + "\n"
                )
            raise e
        with open(fixture_path, "w") as f:
            if stripe_object is not None:
                f.write(str(stripe_object) + "\n")
            else:
                f.write("{}\n")
        return stripe_object

    return _generate_and_save_stripe_fixture


def read_stripe_fixture(
    decorated_function_name: str, mocked_function_name: str
) -> Callable[[Any, Any], Any]:
    def _read_stripe_fixture(*args: Any, **kwargs: Any) -> Any:
        mock = operator.attrgetter(mocked_function_name)(sys.modules[__name__])
        fixture_path = stripe_fixture_path(
            decorated_function_name, mocked_function_name, mock.call_count
        )
        with open(fixture_path, "rb") as f:
            fixture = orjson.loads(f.read())
        # Check for StripeError fixtures
        if "json_body" in fixture:
            requestor = stripe.api_requestor.APIRequestor()
            # This function will raise the relevant StripeError according to the fixture
            requestor.interpret_response(
                fixture["http_body"], fixture["http_status"], fixture["headers"]
            )
        return stripe.util.convert_to_stripe_object(fixture)

    return _read_stripe_fixture


def delete_fixture_data(decorated_function: CallableT) -> None:  # nocoverage
    for fixture_file in fixture_files_for_function(decorated_function):
        os.remove(fixture_file)


def normalize_fixture_data(
    decorated_function: CallableT, tested_timestamp_fields: Sequence[str] = []
) -> None:  # nocoverage
    # stripe ids are all of the form cus_D7OT2jf5YAtZQ2
    id_lengths = [
        ("test", 12),
        ("cus", 14),
        ("prod", 14),
        ("req", 14),
        ("si", 14),
        ("sli", 14),
        ("sub", 14),
        ("acct", 16),
        ("card", 24),
        ("ch", 24),
        ("ii", 24),
        ("il", 24),
        ("in", 24),
        ("pi", 24),
        ("price", 24),
        ("src", 24),
        ("src_client_secret", 24),
        ("tok", 24),
        ("txn", 24),
        ("invst", 26),
        ("rcpt", 31),
    ]
    # We'll replace cus_D7OT2jf5YAtZQ2 with something like cus_NORMALIZED0001
    pattern_translations = {
        f"{prefix}_[A-Za-z0-9]{{{length}}}": f"{prefix}_NORMALIZED%0{length - 10}d"
        for prefix, length in id_lengths
    }
    # We'll replace "invoice_prefix": "A35BC4Q" with something like "invoice_prefix": "NORMA01"
    pattern_translations.update(
        {
            '"invoice_prefix": "([A-Za-z0-9]{7,8})"': "NORMA%02d",
            '"fingerprint": "([A-Za-z0-9]{16})"': "NORMALIZED%06d",
            '"number": "([A-Za-z0-9]{7,8}-[A-Za-z0-9]{4})"': "NORMALI-%04d",
            '"address": "([A-Za-z0-9]{9}-test_[A-Za-z0-9]{12})"': "000000000-test_NORMALIZED%02d",
            # Don't use (..) notation, since the matched strings may be small integers that will also match
            # elsewhere in the file
            '"realm_id": "[0-9]+"': '"realm_id": "%d"',
            r'"account_name": "[\w\s]+"': '"account_name": "NORMALIZED-%d"',
        }
    )
    # Normalizing across all timestamps still causes a lot of variance run to run, which is
    # why we're doing something a bit more complicated
    for i, timestamp_field in enumerate(tested_timestamp_fields):
        # Don't use (..) notation, since the matched timestamp can easily appear in other fields
        pattern_translations[
            f'"{timestamp_field}": 1[5-9][0-9]{{8}}(?![0-9-])'
        ] = f'"{timestamp_field}": 1{i+1:02}%07d'

    normalized_values: Dict[str, Dict[str, str]] = {
        pattern: {} for pattern in pattern_translations.keys()
    }
    for fixture_file in fixture_files_for_function(decorated_function):
        with open(fixture_file) as f:
            file_content = f.read()
        for pattern, translation in pattern_translations.items():
            for match in re.findall(pattern, file_content):
                if match not in normalized_values[pattern]:
                    normalized_values[pattern][match] = translation % (
                        len(normalized_values[pattern]) + 1,
                    )
                file_content = file_content.replace(match, normalized_values[pattern][match])
        file_content = re.sub(r'(?<="risk_score": )(\d+)', "0", file_content)
        file_content = re.sub(r'(?<="times_redeemed": )(\d+)', "0", file_content)
        file_content = re.sub(
            r'(?<="idempotency-key": )"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]*)"',
            '"00000000-0000-0000-0000-000000000000"',
            file_content,
        )
        # Dates
        file_content = re.sub(r'(?<="Date": )"(.* GMT)"', '"NORMALIZED DATETIME"', file_content)
        file_content = re.sub(r"[0-3]\d [A-Z][a-z]{2} 20[1-2]\d", "NORMALIZED DATE", file_content)
        # IP addresses
        file_content = re.sub(r'"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"', '"0.0.0.0"', file_content)
        # All timestamps not in tested_timestamp_fields
        file_content = re.sub(r": (1[5-9][0-9]{8})(?![0-9-])", ": 1000000000", file_content)

        with open(fixture_file, "w") as f:
            f.write(file_content)


MOCKED_STRIPE_FUNCTION_NAMES = [
    f"stripe.{name}"
    for name in [
        "Charge.create",
        "Charge.list",
        "Coupon.create",
        "Customer.create",
        "Customer.retrieve",
        "Customer.save",
        "Invoice.create",
        "Invoice.finalize_invoice",
        "Invoice.list",
        "Invoice.pay",
        "Invoice.refresh",
        "Invoice.upcoming",
        "Invoice.void_invoice",
        "InvoiceItem.create",
        "InvoiceItem.list",
        "Plan.create",
        "Product.create",
        "Subscription.create",
        "Subscription.delete",
        "Subscription.retrieve",
        "Subscription.save",
        "Token.create",
    ]
]


def mock_stripe(
    tested_timestamp_fields: Sequence[str] = [], generate: Optional[bool] = None
) -> Callable[[CallableT], CallableT]:
    def _mock_stripe(decorated_function: CallableT) -> CallableT:
        generate_fixture = generate
        if generate_fixture is None:
            generate_fixture = settings.GENERATE_STRIPE_FIXTURES
        for mocked_function_name in MOCKED_STRIPE_FUNCTION_NAMES:
            mocked_function = operator.attrgetter(mocked_function_name)(sys.modules[__name__])
            if generate_fixture:
                side_effect = generate_and_save_stripe_fixture(
                    decorated_function.__name__, mocked_function_name, mocked_function
                )  # nocoverage
            else:
                side_effect = read_stripe_fixture(decorated_function.__name__, mocked_function_name)
            decorated_function = cast(
                CallableT, patch(mocked_function_name, side_effect=side_effect)(decorated_function)
            )

        @wraps(decorated_function)
        def wrapped(*args: object, **kwargs: object) -> object:
            if generate_fixture:  # nocoverage
                delete_fixture_data(decorated_function)
                val = decorated_function(*args, **kwargs)
                normalize_fixture_data(decorated_function, tested_timestamp_fields)
                return val
            else:
                return decorated_function(*args, **kwargs)

        return cast(CallableT, wrapped)

    return _mock_stripe


class StripeTestCase(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        realm = get_realm("zulip")

        # Explicitly limit our active users to 6 regular users,
        # to make seat_count less prone to changes in our test data.
        # We also keep a guest user and a bot to make the data
        # slightly realistic.
        active_emails = [
            self.example_email("AARON"),
            self.example_email("cordelia"),
            self.example_email("hamlet"),
            self.example_email("iago"),
            self.example_email("othello"),
            self.example_email("desdemona"),
            self.example_email("polonius"),  # guest
            self.example_email("default_bot"),  # bot
        ]

        # Deactivate all users in our realm that aren't in our whitelist.
        for user_profile in UserProfile.objects.filter(realm_id=realm.id).exclude(
            delivery_email__in=active_emails
        ):
            do_deactivate_user(user_profile, acting_user=None)

        # sanity check our 8 expected users are active
        self.assertEqual(
            UserProfile.objects.filter(realm=realm, is_active=True).count(),
            8,
        )

        # Make sure we have active users outside our realm (to make
        # sure relevant queries restrict on realm).
        self.assertEqual(
            UserProfile.objects.exclude(realm=realm).filter(is_active=True).count(),
            10,
        )

        # Our seat count excludes our guest user and bot, and
        # we want this to be predictable for certain tests with
        # arithmetic calculations.
        self.assertEqual(get_latest_seat_count(realm), 6)
        self.seat_count = 6
        self.signed_seat_count, self.salt = sign_string(str(self.seat_count))
        # Choosing dates with corresponding timestamps below 1500000000 so that they are
        # not caught by our timestamp normalization regex in normalize_fixture_data
        self.now = datetime(2012, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.next_month = datetime(2012, 2, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.next_year = datetime(2013, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def get_signed_seat_count_from_response(self, response: HttpResponse) -> Optional[str]:
        match = re.search(r"name=\"signed_seat_count\" value=\"(.+)\"", response.content.decode())
        return match.group(1) if match else None

    def get_salt_from_response(self, response: HttpResponse) -> Optional[str]:
        match = re.search(r"name=\"salt\" value=\"(\w+)\"", response.content.decode())
        return match.group(1) if match else None

    def upgrade(
        self,
        invoice: bool = False,
        talk_to_stripe: bool = True,
        realm: Optional[Realm] = None,
        del_args: Sequence[str] = [],
        **kwargs: Any,
    ) -> HttpResponse:
        host_args = {}
        if realm is not None:  # nocoverage: TODO
            host_args["HTTP_HOST"] = realm.host
        response = self.client_get("/upgrade/", {}, **host_args)
        params: Dict[str, Any] = {
            "schedule": "annual",
            "signed_seat_count": self.get_signed_seat_count_from_response(response),
            "salt": self.get_salt_from_response(response),
        }
        if invoice:  # send_invoice
            params.update(
                billing_modality="send_invoice",
                licenses=kwargs.get("licenses", 123),
            )
        else:  # charge_automatically
            stripe_token = None
            if not talk_to_stripe:
                stripe_token = "token"
            stripe_token = kwargs.get("stripe_token", stripe_token)
            if stripe_token is None:
                stripe_token = stripe_create_token().id
            params.update(
                billing_modality="charge_automatically",
                license_management="automatic",
                stripe_token=stripe_token,
            )

        params.update(kwargs)
        for key in del_args:
            if key in params:
                del params[key]
        return self.client_post("/json/billing/upgrade", params, **host_args)

    # Upgrade without talking to Stripe
    def local_upgrade(self, *args: Any) -> None:
        class StripeMock(Mock):
            def __init__(self, depth: int = 1):
                super().__init__(spec=stripe.Card)
                self.id = "id"
                self.created = "1000"
                self.last4 = "4242"
                if depth == 1:
                    self.source = StripeMock(depth=2)

        def upgrade_func(*args: Any) -> Any:
            return process_initial_upgrade(self.example_user("hamlet"), *args[:4])

        for mocked_function_name in MOCKED_STRIPE_FUNCTION_NAMES:
            upgrade_func = patch(mocked_function_name, return_value=StripeMock())(upgrade_func)
        upgrade_func(*args)


class StripeTest(StripeTestCase):
    def test_catch_stripe_errors(self) -> None:
        @catch_stripe_errors
        def raise_invalid_request_error() -> None:
            raise stripe.error.InvalidRequestError("message", "param", "code", json_body={})

        with self.assertLogs("corporate.stripe", "ERROR") as error_log:
            with self.assertRaises(BillingError) as billing_context:
                raise_invalid_request_error()
            self.assertEqual("other stripe error", billing_context.exception.error_description)
            self.assertEqual(
                error_log.output, ["ERROR:corporate.stripe:Stripe error: None None None None"]
            )

        @catch_stripe_errors
        def raise_card_error() -> None:
            error_message = "The card number is not a valid credit card number."
            json_body = {"error": {"message": error_message}}
            raise stripe.error.CardError(
                error_message, "number", "invalid_number", json_body=json_body
            )

        with self.assertLogs("corporate.stripe", "INFO") as info_log:
            with self.assertRaises(StripeCardError) as card_context:
                raise_card_error()
            self.assertIn("not a valid credit card", str(card_context.exception))
            self.assertEqual("card error", card_context.exception.error_description)
            self.assertEqual(
                info_log.output, ["INFO:corporate.stripe:Stripe card error: None None None None"]
            )

    def test_billing_not_enabled(self) -> None:
        iago = self.example_user("iago")
        with self.settings(BILLING_ENABLED=False):
            self.login_user(iago)
            response = self.client_get("/upgrade/", follow=True)
            self.assertEqual(response.status_code, 404)

    @mock_stripe(tested_timestamp_fields=["created"])
    def test_upgrade_by_card(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        response = self.client_get("/upgrade/")
        self.assert_in_success_response(["Pay annually"], response)
        self.assertNotEqual(user.realm.plan_type, Realm.STANDARD)
        self.assertFalse(Customer.objects.filter(realm=user.realm).exists())

        # Click "Make payment" in Stripe Checkout
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade()

        # Check that we correctly created a Customer object in Stripe
        stripe_customer = stripe_get_customer(
            assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
        )
        self.assertEqual(stripe_customer.default_source.id[:5], "card_")
        self.assertTrue(stripe_customer_has_credit_card_as_default_source(stripe_customer))
        self.assertEqual(stripe_customer.description, "zulip (Zulip Dev)")
        self.assertEqual(stripe_customer.discount, None)
        self.assertEqual(stripe_customer.email, user.delivery_email)
        metadata_dict = dict(stripe_customer.metadata)
        self.assertEqual(metadata_dict["realm_str"], "zulip")
        try:
            int(metadata_dict["realm_id"])
        except ValueError:  # nocoverage
            raise AssertionError("realm_id is not a number")

        # Check Charges in Stripe
        [charge] = stripe.Charge.list(customer=stripe_customer.id)
        self.assertEqual(charge.amount, 8000 * self.seat_count)
        # TODO: fix Decimal
        self.assertEqual(
            charge.description, f"Upgrade to Zulip Standard, $80.0 x {self.seat_count}"
        )
        self.assertEqual(charge.receipt_email, user.delivery_email)
        self.assertEqual(charge.statement_descriptor, "Zulip Standard")
        # Check Invoices in Stripe
        [invoice] = stripe.Invoice.list(customer=stripe_customer.id)
        self.assertIsNotNone(invoice.status_transitions.finalized_at)
        invoice_params = {
            # auto_advance is False because the invoice has been paid
            "amount_due": 0,
            "amount_paid": 0,
            "auto_advance": False,
            "collection_method": "charge_automatically",
            "charge": None,
            "status": "paid",
            "total": 0,
        }
        for key, value in invoice_params.items():
            self.assertEqual(invoice.get(key), value)
        # Check Line Items on Stripe Invoice
        [item0, item1] = invoice.lines
        line_item_params = {
            "amount": 8000 * self.seat_count,
            "description": "Zulip Standard",
            "discountable": False,
            "period": {
                "end": datetime_to_timestamp(self.next_year),
                "start": datetime_to_timestamp(self.now),
            },
            # There's no unit_amount on Line Items, probably because it doesn't show up on the
            # user-facing invoice. We could pull the Invoice Item instead and test unit_amount there,
            # but testing the amount and quantity seems sufficient.
            "plan": None,
            "proration": False,
            "quantity": self.seat_count,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item0.get(key), value)
        line_item_params = {
            "amount": -8000 * self.seat_count,
            "description": "Payment (Card ending in 4242)",
            "discountable": False,
            "plan": None,
            "proration": False,
            "quantity": 1,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item1.get(key), value)

        # Check that we correctly populated Customer, CustomerPlan, and LicenseLedger in Zulip
        customer = Customer.objects.get(stripe_customer_id=stripe_customer.id, realm=user.realm)
        plan = CustomerPlan.objects.get(
            customer=customer,
            automanage_licenses=True,
            price_per_license=8000,
            fixed_price=None,
            discount=None,
            billing_cycle_anchor=self.now,
            billing_schedule=CustomerPlan.ANNUAL,
            invoiced_through=LicenseLedger.objects.first(),
            next_invoice_date=self.next_month,
            tier=CustomerPlan.STANDARD,
            status=CustomerPlan.ACTIVE,
        )
        LicenseLedger.objects.get(
            plan=plan,
            is_renewal=True,
            event_time=self.now,
            licenses=self.seat_count,
            licenses_at_next_renewal=self.seat_count,
        )
        # Check RealmAuditLog
        audit_log_entries = list(
            RealmAuditLog.objects.filter(acting_user=user)
            .values_list("event_type", "event_time")
            .order_by("id")
        )
        self.assertEqual(
            audit_log_entries[:3],
            [
                (
                    RealmAuditLog.STRIPE_CUSTOMER_CREATED,
                    timestamp_to_datetime(stripe_customer.created),
                ),
                (RealmAuditLog.STRIPE_CARD_CHANGED, timestamp_to_datetime(stripe_customer.created)),
                (RealmAuditLog.CUSTOMER_PLAN_CREATED, self.now),
            ],
        )
        self.assertEqual(audit_log_entries[3][0], RealmAuditLog.REALM_PLAN_TYPE_CHANGED)
        self.assertEqual(
            orjson.loads(
                assert_is_not_none(
                    RealmAuditLog.objects.filter(event_type=RealmAuditLog.CUSTOMER_PLAN_CREATED)
                    .values_list("extra_data", flat=True)
                    .first()
                )
            )["automanage_licenses"],
            True,
        )
        # Check that we correctly updated Realm
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        # Check that we can no longer access /upgrade
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/billing/", response.url)

        # Check /billing has the correct information
        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            response = self.client_get("/billing/")
        self.assert_not_in_success_response(["Pay annually"], response)
        for substring in [
            "Zulip Standard",
            str(self.seat_count),
            "You are using",
            f"{self.seat_count} of {self.seat_count} licenses",
            "Licenses are automatically managed by Zulip; when you add",
            "Your plan will renew on",
            "January 2, 2013",
            f"${80 * self.seat_count}.00",
            f"Billing email: <strong>{user.delivery_email}</strong>",
            "Visa ending in 4242",
            "Update card",
        ]:
            self.assert_in_response(substring, response)

        self.assert_not_in_success_response(
            [
                "You can only increase the number of licenses.",
                "Number of licenses",
                "Licenses in next renewal",
            ],
            response,
        )

    @mock_stripe(tested_timestamp_fields=["created"])
    def test_upgrade_by_invoice(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        # Click "Make payment" in Stripe Checkout
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade(invoice=True)
        # Check that we correctly created a Customer in Stripe
        stripe_customer = stripe_get_customer(
            assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
        )
        self.assertFalse(stripe_customer_has_credit_card_as_default_source(stripe_customer))
        # It can take a second for Stripe to attach the source to the customer, and in
        # particular it may not be attached at the time stripe_get_customer is called above,
        # causing test flakes.
        # So commenting the next line out, but leaving it here so future readers know what
        # is supposed to happen here
        # self.assertEqual(stripe_customer.default_source.type, 'ach_credit_transfer')

        # Check Charges in Stripe
        self.assertFalse(stripe.Charge.list(customer=stripe_customer.id))
        # Check Invoices in Stripe
        [invoice] = stripe.Invoice.list(customer=stripe_customer.id)
        self.assertIsNotNone(invoice.due_date)
        self.assertIsNotNone(invoice.status_transitions.finalized_at)
        invoice_params = {
            "amount_due": 8000 * 123,
            "amount_paid": 0,
            "attempt_count": 0,
            "auto_advance": True,
            "collection_method": "send_invoice",
            "statement_descriptor": "Zulip Standard",
            "status": "open",
            "total": 8000 * 123,
        }
        for key, value in invoice_params.items():
            self.assertEqual(invoice.get(key), value)
        # Check Line Items on Stripe Invoice
        [item] = invoice.lines
        line_item_params = {
            "amount": 8000 * 123,
            "description": "Zulip Standard",
            "discountable": False,
            "period": {
                "end": datetime_to_timestamp(self.next_year),
                "start": datetime_to_timestamp(self.now),
            },
            "plan": None,
            "proration": False,
            "quantity": 123,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item.get(key), value)

        # Check that we correctly populated Customer, CustomerPlan and LicenseLedger in Zulip
        customer = Customer.objects.get(stripe_customer_id=stripe_customer.id, realm=user.realm)
        plan = CustomerPlan.objects.get(
            customer=customer,
            automanage_licenses=False,
            charge_automatically=False,
            price_per_license=8000,
            fixed_price=None,
            discount=None,
            billing_cycle_anchor=self.now,
            billing_schedule=CustomerPlan.ANNUAL,
            invoiced_through=LicenseLedger.objects.first(),
            next_invoice_date=self.next_year,
            tier=CustomerPlan.STANDARD,
            status=CustomerPlan.ACTIVE,
        )
        LicenseLedger.objects.get(
            plan=plan,
            is_renewal=True,
            event_time=self.now,
            licenses=123,
            licenses_at_next_renewal=123,
        )
        # Check RealmAuditLog
        audit_log_entries = list(
            RealmAuditLog.objects.filter(acting_user=user)
            .values_list("event_type", "event_time")
            .order_by("id")
        )
        self.assertEqual(
            audit_log_entries[:2],
            [
                (
                    RealmAuditLog.STRIPE_CUSTOMER_CREATED,
                    timestamp_to_datetime(stripe_customer.created),
                ),
                (RealmAuditLog.CUSTOMER_PLAN_CREATED, self.now),
            ],
        )
        self.assertEqual(audit_log_entries[2][0], RealmAuditLog.REALM_PLAN_TYPE_CHANGED)
        self.assertEqual(
            orjson.loads(
                assert_is_not_none(
                    RealmAuditLog.objects.filter(event_type=RealmAuditLog.CUSTOMER_PLAN_CREATED)
                    .values_list("extra_data", flat=True)
                    .first()
                )
            )["automanage_licenses"],
            False,
        )
        # Check that we correctly updated Realm
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.STANDARD)
        self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)
        # Check that we can no longer access /upgrade
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/billing/", response.url)

        # Check /billing has the correct information
        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            response = self.client_get("/billing/")
        self.assert_not_in_success_response(["Pay annually", "Update card"], response)
        for substring in [
            "Zulip Standard",
            str(123),
            "You are using",
            f"{self.seat_count} of {123} licenses",
            "Licenses are manually managed. You will not be able to add ",
            "Your plan will renew on",
            "January 2, 2013",
            "$9,840.00",  # 9840 = 80 * 123
            f"Billing email: <strong>{user.delivery_email}</strong>",
            "Billed by invoice",
            "You can only increase the number of licenses.",
            "Number of licenses",
            "Licenses in next renewal",
        ]:
            self.assert_in_response(substring, response)

    @mock_stripe(tested_timestamp_fields=["created"])
    def test_free_trial_upgrade_by_card(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)

        with self.settings(FREE_TRIAL_DAYS=60):
            response = self.client_get("/upgrade/")
            free_trial_end_date = self.now + timedelta(days=60)

            self.assert_in_success_response(["Pay annually", "Free Trial", "60 day"], response)
            self.assertNotEqual(user.realm.plan_type, Realm.STANDARD)
            self.assertFalse(Customer.objects.filter(realm=user.realm).exists())

            with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
                self.upgrade()

            stripe_customer = stripe_get_customer(
                assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
            )
            self.assertEqual(stripe_customer.default_source.id[:5], "card_")
            self.assertEqual(stripe_customer.description, "zulip (Zulip Dev)")
            self.assertEqual(stripe_customer.discount, None)
            self.assertEqual(stripe_customer.email, user.delivery_email)
            metadata_dict = dict(stripe_customer.metadata)
            self.assertEqual(metadata_dict["realm_str"], "zulip")
            try:
                int(metadata_dict["realm_id"])
            except ValueError:  # nocoverage
                raise AssertionError("realm_id is not a number")

            self.assertFalse(stripe.Charge.list(customer=stripe_customer.id))

            self.assertFalse(stripe.Invoice.list(customer=stripe_customer.id))

            customer = Customer.objects.get(stripe_customer_id=stripe_customer.id, realm=user.realm)
            plan = CustomerPlan.objects.get(
                customer=customer,
                automanage_licenses=True,
                price_per_license=8000,
                fixed_price=None,
                discount=None,
                billing_cycle_anchor=self.now,
                billing_schedule=CustomerPlan.ANNUAL,
                invoiced_through=LicenseLedger.objects.first(),
                next_invoice_date=free_trial_end_date,
                tier=CustomerPlan.STANDARD,
                status=CustomerPlan.FREE_TRIAL,
            )
            LicenseLedger.objects.get(
                plan=plan,
                is_renewal=True,
                event_time=self.now,
                licenses=self.seat_count,
                licenses_at_next_renewal=self.seat_count,
            )
            audit_log_entries = list(
                RealmAuditLog.objects.filter(acting_user=user)
                .values_list("event_type", "event_time")
                .order_by("id")
            )
            self.assertEqual(
                audit_log_entries[:3],
                [
                    (
                        RealmAuditLog.STRIPE_CUSTOMER_CREATED,
                        timestamp_to_datetime(stripe_customer.created),
                    ),
                    (
                        RealmAuditLog.STRIPE_CARD_CHANGED,
                        timestamp_to_datetime(stripe_customer.created),
                    ),
                    (RealmAuditLog.CUSTOMER_PLAN_CREATED, self.now),
                ],
            )
            self.assertEqual(audit_log_entries[3][0], RealmAuditLog.REALM_PLAN_TYPE_CHANGED)
            self.assertEqual(
                orjson.loads(
                    assert_is_not_none(
                        RealmAuditLog.objects.filter(event_type=RealmAuditLog.CUSTOMER_PLAN_CREATED)
                        .values_list("extra_data", flat=True)
                        .first()
                    )
                )["automanage_licenses"],
                True,
            )

            realm = get_realm("zulip")
            self.assertEqual(realm.plan_type, Realm.STANDARD)
            self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)

            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_get("/billing/")
            self.assert_not_in_success_response(["Pay annually"], response)
            for substring in [
                "Zulip Standard",
                "Free Trial",
                str(self.seat_count),
                "You are using",
                f"{self.seat_count} of {self.seat_count} licenses",
                "Your plan will be upgraded to",
                "March 2, 2012",
                f"${80 * self.seat_count}.00",
                f"Billing email: <strong>{user.delivery_email}</strong>",
                "Visa ending in 4242",
                "Update card",
            ]:
                self.assert_in_response(substring, response)
            self.assert_not_in_success_response(["Go to your Zulip organization"], response)

            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_get("/billing/", {"onboarding": "true"})
                self.assert_in_success_response(["Go to your Zulip organization"], response)

            with patch("corporate.lib.stripe.get_latest_seat_count", return_value=12):
                update_license_ledger_if_needed(realm, self.now)
            self.assertEqual(
                LicenseLedger.objects.order_by("-id")
                .values_list("licenses", "licenses_at_next_renewal")
                .first(),
                (12, 12),
            )

            with patch("corporate.lib.stripe.get_latest_seat_count", return_value=15):
                update_license_ledger_if_needed(realm, self.next_month)
            self.assertEqual(
                LicenseLedger.objects.order_by("-id")
                .values_list("licenses", "licenses_at_next_renewal")
                .first(),
                (15, 15),
            )

            invoice_plans_as_needed(self.next_month)
            self.assertFalse(stripe.Invoice.list(customer=stripe_customer.id))
            customer_plan = CustomerPlan.objects.get(customer=customer)
            self.assertEqual(customer_plan.status, CustomerPlan.FREE_TRIAL)
            self.assertEqual(customer_plan.next_invoice_date, free_trial_end_date)

            invoice_plans_as_needed(free_trial_end_date)
            customer_plan.refresh_from_db()
            realm.refresh_from_db()
            self.assertEqual(customer_plan.status, CustomerPlan.ACTIVE)
            self.assertEqual(customer_plan.next_invoice_date, add_months(free_trial_end_date, 1))
            self.assertEqual(realm.plan_type, Realm.STANDARD)
            [invoice] = stripe.Invoice.list(customer=stripe_customer.id)
            invoice_params = {
                "amount_due": 15 * 80 * 100,
                "amount_paid": 0,
                "amount_remaining": 15 * 80 * 100,
                "auto_advance": True,
                "collection_method": "charge_automatically",
                "customer_email": self.example_email("hamlet"),
                "discount": None,
                "paid": False,
                "status": "open",
                "total": 15 * 80 * 100,
            }
            for key, value in invoice_params.items():
                self.assertEqual(invoice.get(key), value)
            [invoice_item] = invoice.get("lines")
            invoice_item_params = {
                "amount": 15 * 80 * 100,
                "description": "Zulip Standard - renewal",
                "plan": None,
                "quantity": 15,
                "subscription": None,
                "discountable": False,
                "period": {
                    "start": datetime_to_timestamp(free_trial_end_date),
                    "end": datetime_to_timestamp(add_months(free_trial_end_date, 12)),
                },
            }
            for key, value in invoice_item_params.items():
                self.assertEqual(invoice_item[key], value)

            invoice_plans_as_needed(add_months(free_trial_end_date, 1))
            [invoice] = stripe.Invoice.list(customer=stripe_customer.id)

            with patch("corporate.lib.stripe.get_latest_seat_count", return_value=19):
                update_license_ledger_if_needed(realm, add_months(free_trial_end_date, 10))
            self.assertEqual(
                LicenseLedger.objects.order_by("-id")
                .values_list("licenses", "licenses_at_next_renewal")
                .first(),
                (19, 19),
            )
            invoice_plans_as_needed(add_months(free_trial_end_date, 10))
            [invoice0, invoice1] = stripe.Invoice.list(customer=stripe_customer.id)
            invoice_params = {
                "amount_due": 5172,
                "auto_advance": True,
                "collection_method": "charge_automatically",
                "customer_email": "hamlet@zulip.com",
            }
            [invoice_item] = invoice0.get("lines")
            invoice_item_params = {
                "amount": 5172,
                "description": "Additional license (Jan 2, 2013 - Mar 2, 2013)",
                "discountable": False,
                "quantity": 4,
                "period": {
                    "start": datetime_to_timestamp(add_months(free_trial_end_date, 10)),
                    "end": datetime_to_timestamp(add_months(free_trial_end_date, 12)),
                },
            }

            invoice_plans_as_needed(add_months(free_trial_end_date, 12))
            [invoice0, invoice1, invoice2] = stripe.Invoice.list(customer=stripe_customer.id)

    @mock_stripe(tested_timestamp_fields=["created"])
    def test_free_trial_upgrade_by_invoice(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)

        free_trial_end_date = self.now + timedelta(days=60)
        with self.settings(FREE_TRIAL_DAYS=60):
            response = self.client_get("/upgrade/")

            self.assert_in_success_response(["Pay annually", "Free Trial", "60 day"], response)
            self.assertNotEqual(user.realm.plan_type, Realm.STANDARD)
            self.assertFalse(Customer.objects.filter(realm=user.realm).exists())

            with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
                self.upgrade(invoice=True)

            stripe_customer = stripe_get_customer(
                assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
            )
            self.assertEqual(stripe_customer.discount, None)
            self.assertEqual(stripe_customer.email, user.delivery_email)
            metadata_dict = dict(stripe_customer.metadata)
            self.assertEqual(metadata_dict["realm_str"], "zulip")
            try:
                int(metadata_dict["realm_id"])
            except ValueError:  # nocoverage
                raise AssertionError("realm_id is not a number")

            self.assertFalse(stripe.Invoice.list(customer=stripe_customer.id))

            customer = Customer.objects.get(stripe_customer_id=stripe_customer.id, realm=user.realm)
            plan = CustomerPlan.objects.get(
                customer=customer,
                automanage_licenses=False,
                price_per_license=8000,
                fixed_price=None,
                discount=None,
                billing_cycle_anchor=self.now,
                billing_schedule=CustomerPlan.ANNUAL,
                invoiced_through=LicenseLedger.objects.first(),
                next_invoice_date=free_trial_end_date,
                tier=CustomerPlan.STANDARD,
                status=CustomerPlan.FREE_TRIAL,
            )

            LicenseLedger.objects.get(
                plan=plan,
                is_renewal=True,
                event_time=self.now,
                licenses=123,
                licenses_at_next_renewal=123,
            )
            audit_log_entries = list(
                RealmAuditLog.objects.filter(acting_user=user)
                .values_list("event_type", "event_time")
                .order_by("id")
            )
            self.assertEqual(
                audit_log_entries[:2],
                [
                    (
                        RealmAuditLog.STRIPE_CUSTOMER_CREATED,
                        timestamp_to_datetime(stripe_customer.created),
                    ),
                    (RealmAuditLog.CUSTOMER_PLAN_CREATED, self.now),
                ],
            )
            self.assertEqual(audit_log_entries[2][0], RealmAuditLog.REALM_PLAN_TYPE_CHANGED)
            self.assertEqual(
                orjson.loads(
                    assert_is_not_none(
                        RealmAuditLog.objects.filter(event_type=RealmAuditLog.CUSTOMER_PLAN_CREATED)
                        .values_list("extra_data", flat=True)
                        .first()
                    )
                )["automanage_licenses"],
                False,
            )

            realm = get_realm("zulip")
            self.assertEqual(realm.plan_type, Realm.STANDARD)
            self.assertEqual(realm.max_invites, Realm.INVITES_STANDARD_REALM_DAILY_MAX)

            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_get("/billing/")
            self.assert_not_in_success_response(["Pay annually"], response)
            for substring in [
                "Zulip Standard",
                "Free Trial",
                str(self.seat_count),
                "You are using",
                f"{self.seat_count} of {123} licenses",
                "Your plan will be upgraded to",
                "March 2, 2012",
                f"{80 * 123:,.2f}",
                f"Billing email: <strong>{user.delivery_email}</strong>",
                "Billed by invoice",
            ]:
                self.assert_in_response(substring, response)

            with patch("corporate.lib.stripe.invoice_plan") as mocked:
                invoice_plans_as_needed(self.next_month)
            mocked.assert_not_called()
            mocked.reset_mock()
            customer_plan = CustomerPlan.objects.get(customer=customer)
            self.assertEqual(customer_plan.status, CustomerPlan.FREE_TRIAL)
            self.assertEqual(customer_plan.next_invoice_date, free_trial_end_date)

            invoice_plans_as_needed(free_trial_end_date)
            customer_plan.refresh_from_db()
            realm.refresh_from_db()
            self.assertEqual(customer_plan.status, CustomerPlan.ACTIVE)
            self.assertEqual(customer_plan.next_invoice_date, add_months(free_trial_end_date, 12))
            self.assertEqual(realm.plan_type, Realm.STANDARD)
            [invoice] = stripe.Invoice.list(customer=stripe_customer.id)
            invoice_params = {
                "amount_due": 123 * 80 * 100,
                "amount_paid": 0,
                "amount_remaining": 123 * 80 * 100,
                "auto_advance": True,
                "collection_method": "send_invoice",
                "customer_email": self.example_email("hamlet"),
                "discount": None,
                "paid": False,
                "status": "open",
                "total": 123 * 80 * 100,
            }
            for key, value in invoice_params.items():
                self.assertEqual(invoice.get(key), value)
            [invoice_item] = invoice.get("lines")
            invoice_item_params = {
                "amount": 123 * 80 * 100,
                "description": "Zulip Standard - renewal",
                "plan": None,
                "quantity": 123,
                "subscription": None,
                "discountable": False,
                "period": {
                    "start": datetime_to_timestamp(free_trial_end_date),
                    "end": datetime_to_timestamp(add_months(free_trial_end_date, 12)),
                },
            }
            for key, value in invoice_item_params.items():
                self.assertEqual(invoice_item[key], value)

            invoice_plans_as_needed(add_months(free_trial_end_date, 1))
            [invoice] = stripe.Invoice.list(customer=stripe_customer.id)

            invoice_plans_as_needed(add_months(free_trial_end_date, 10))
            [invoice] = stripe.Invoice.list(customer=stripe_customer.id)

            invoice_plans_as_needed(add_months(free_trial_end_date, 12))
            [invoice0, invoice1] = stripe.Invoice.list(customer=stripe_customer.id)

    @mock_stripe()
    def test_billing_page_permissions(self, *mocks: Mock) -> None:
        # Guest users can't access /upgrade page
        self.login_user(self.example_user("polonius"))
        response = self.client_get("/upgrade/", follow=True)
        self.assertEqual(response.status_code, 404)

        # Check that non-admins can access /upgrade via /billing, when there is no Customer object
        self.login_user(self.example_user("hamlet"))
        response = self.client_get("/billing/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/upgrade/", response.url)
        # Check that non-admins can sign up and pay
        self.upgrade()
        # Check that the non-admin hamlet can still access /billing
        response = self.client_get("/billing/")
        self.assert_in_success_response(["Your current plan is"], response)

        # Check realm owners can access billing, even though they are not a billing admin
        desdemona = self.example_user("desdemona")
        desdemona.role = UserProfile.ROLE_REALM_OWNER
        desdemona.save(update_fields=["role"])
        self.login_user(self.example_user("desdemona"))
        response = self.client_get("/billing/")
        self.assert_in_success_response(["Your current plan is"], response)

        # Check that member who is not a billing admin does not have access
        self.login_user(self.example_user("cordelia"))
        response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["You must be an organization owner or a billing administrator"], response
        )

    @mock_stripe(tested_timestamp_fields=["created"])
    def test_upgrade_by_card_with_outdated_seat_count(self, *mocks: Mock) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        new_seat_count = 23
        # Change the seat count while the user is going through the upgrade flow
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=new_seat_count):
            self.upgrade()
        customer = Customer.objects.first()
        assert customer is not None
        stripe_customer_id: str = assert_is_not_none(customer.stripe_customer_id)
        # Check that the Charge used the old quantity, not new_seat_count
        [charge] = stripe.Charge.list(customer=stripe_customer_id)
        self.assertEqual(8000 * self.seat_count, charge.amount)
        # Check that the invoice has a credit for the old amount and a charge for the new one
        [stripe_invoice] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual(
            [8000 * new_seat_count, -8000 * self.seat_count],
            [item.amount for item in stripe_invoice.lines],
        )
        # Check LicenseLedger has the new amount
        ledger_entry = LicenseLedger.objects.first()
        assert ledger_entry is not None
        self.assertEqual(ledger_entry.licenses, new_seat_count)
        self.assertEqual(ledger_entry.licenses_at_next_renewal, new_seat_count)

    @mock_stripe()
    def test_upgrade_where_first_card_fails(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        # From https://stripe.com/docs/testing#cards: Attaching this card to
        # a Customer object succeeds, but attempts to charge the customer fail.
        with self.assertLogs("corporate.stripe", "INFO") as m:
            self.upgrade(stripe_token=stripe_create_token("4000000000000341").id)
            self.assertEqual(
                m.output,
                ["INFO:corporate.stripe:Stripe card error: 402 card_error card_declined None"],
            )
        # Check that we created a Customer object but no CustomerPlan
        stripe_customer_id = Customer.objects.get(realm=get_realm("zulip")).stripe_customer_id
        assert stripe_customer_id is not None
        self.assertFalse(CustomerPlan.objects.exists())
        # Check that we created a Customer in stripe, a failed Charge, and no Invoices or Invoice Items
        self.assertTrue(stripe_get_customer(stripe_customer_id))
        [charge] = stripe.Charge.list(customer=stripe_customer_id)
        self.assertEqual(charge.failure_code, "card_declined")
        # TODO: figure out what these actually are
        self.assertFalse(stripe.Invoice.list(customer=stripe_customer_id))
        self.assertFalse(stripe.InvoiceItem.list(customer=stripe_customer_id))
        # Check that we correctly populated RealmAuditLog
        audit_log_entries = list(
            RealmAuditLog.objects.filter(acting_user=user)
            .values_list("event_type", flat=True)
            .order_by("id")
        )
        self.assertEqual(
            audit_log_entries,
            [RealmAuditLog.STRIPE_CUSTOMER_CREATED, RealmAuditLog.STRIPE_CARD_CHANGED],
        )
        # Check that we did not update Realm
        realm = get_realm("zulip")
        self.assertNotEqual(realm.plan_type, Realm.STANDARD)
        # Check that we still get redirected to /upgrade
        response = self.client_get("/billing/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/upgrade/", response.url)

        # Try again, with a valid card, after they added a few users
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=23):
            with patch("corporate.views.upgrade.get_latest_seat_count", return_value=23):
                self.upgrade()
        customer = Customer.objects.get(realm=get_realm("zulip"))
        # It's impossible to create two Customers, but check that we didn't
        # change stripe_customer_id
        self.assertEqual(customer.stripe_customer_id, stripe_customer_id)
        # Check that we successfully added a CustomerPlan, and have the right number of licenses
        plan = CustomerPlan.objects.get(customer=customer)
        ledger_entry = LicenseLedger.objects.get(plan=plan)
        self.assertEqual(ledger_entry.licenses, 23)
        self.assertEqual(ledger_entry.licenses_at_next_renewal, 23)
        # Check the Charges and Invoices in Stripe
        [charge0, charge1] = stripe.Charge.list(customer=stripe_customer_id)
        self.assertEqual(8000 * 23, charge0.amount)
        [stripe_invoice] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual([8000 * 23, -8000 * 23], [item.amount for item in stripe_invoice.lines])
        # Check that we correctly populated RealmAuditLog
        audit_log_entries = list(
            RealmAuditLog.objects.filter(acting_user=user)
            .values_list("event_type", flat=True)
            .order_by("id")
        )
        self.assertEqual(
            audit_log_entries,
            [
                RealmAuditLog.STRIPE_CUSTOMER_CREATED,
                RealmAuditLog.STRIPE_CARD_CHANGED,
                RealmAuditLog.STRIPE_CARD_CHANGED,
                RealmAuditLog.CUSTOMER_PLAN_CREATED,
                RealmAuditLog.REALM_PLAN_TYPE_CHANGED,
            ],
        )
        # Check that we correctly updated Realm
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.STANDARD)
        # Check that we can no longer access /upgrade
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/billing/", response.url)

    def test_upgrade_with_tampered_seat_count(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        response = self.upgrade(talk_to_stripe=False, salt="badsalt")
        self.assert_json_error_contains(response, "Something went wrong. Please contact")
        self.assertEqual(orjson.loads(response.content)["error_description"], "tampered seat count")

    def test_upgrade_race_condition(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        with self.assertLogs("corporate.stripe", "WARNING") as m:
            with self.assertRaises(BillingError) as context:
                self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        self.assertEqual(
            "subscribing with existing subscription", context.exception.error_description
        )
        self.assertEqual(
            m.output[0],
            f"WARNING:corporate.stripe:Customer <Customer <Realm: zulip {hamlet.realm.id}> id> trying to upgrade, but has an active subscription",
        )
        self.assert_length(m.output, 1)

    def test_check_upgrade_parameters(self) -> None:
        # Tests all the error paths except 'not enough licenses'
        def check_error(
            error_message: str,
            error_description: str,
            upgrade_params: Mapping[str, Any],
            del_args: Sequence[str] = [],
        ) -> None:
            response = self.upgrade(talk_to_stripe=False, del_args=del_args, **upgrade_params)
            self.assert_json_error_contains(response, error_message)
            if error_description:
                self.assertEqual(
                    orjson.loads(response.content)["error_description"], error_description
                )

        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        check_error("Invalid billing_modality", "", {"billing_modality": "invalid"})
        check_error("Invalid schedule", "", {"schedule": "invalid"})
        check_error("Invalid license_management", "", {"license_management": "invalid"})
        check_error(
            "Something went wrong. Please contact",
            "autopay with no card",
            {},
            del_args=["stripe_token"],
        )

    def test_upgrade_license_counts(self) -> None:
        def check_min_licenses_error(
            invoice: bool,
            licenses: Optional[int],
            min_licenses_in_response: int,
            upgrade_params: Dict[str, Any] = {},
        ) -> None:
            if licenses is None:
                del_args = ["licenses"]
            else:
                del_args = []
                upgrade_params["licenses"] = licenses
            response = self.upgrade(
                invoice=invoice, talk_to_stripe=False, del_args=del_args, **upgrade_params
            )
            self.assert_json_error_contains(response, f"at least {min_licenses_in_response} users")
            self.assertEqual(
                orjson.loads(response.content)["error_description"], "not enough licenses"
            )

        def check_max_licenses_error(licenses: int) -> None:
            response = self.upgrade(invoice=True, talk_to_stripe=False, licenses=licenses)
            self.assert_json_error_contains(
                response, f"with more than {MAX_INVOICED_LICENSES} licenses"
            )
            self.assertEqual(
                orjson.loads(response.content)["error_description"], "too many licenses"
            )

        def check_success(
            invoice: bool, licenses: Optional[int], upgrade_params: Dict[str, Any] = {}
        ) -> None:
            if licenses is None:
                del_args = ["licenses"]
            else:
                del_args = []
                upgrade_params["licenses"] = licenses
            with patch("corporate.views.upgrade.process_initial_upgrade"):
                response = self.upgrade(
                    invoice=invoice, talk_to_stripe=False, del_args=del_args, **upgrade_params
                )
            self.assert_json_success(response)

        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        # Autopay with licenses < seat count
        check_min_licenses_error(
            False, self.seat_count - 1, self.seat_count, {"license_management": "manual"}
        )
        # Autopay with not setting licenses
        check_min_licenses_error(False, None, self.seat_count, {"license_management": "manual"})
        # Invoice with licenses < MIN_INVOICED_LICENSES
        check_min_licenses_error(True, MIN_INVOICED_LICENSES - 1, MIN_INVOICED_LICENSES)
        # Invoice with licenses < seat count
        with patch("corporate.lib.stripe.MIN_INVOICED_LICENSES", 3):
            check_min_licenses_error(True, 4, self.seat_count)
        # Invoice with not setting licenses
        check_min_licenses_error(True, None, MIN_INVOICED_LICENSES)
        # Invoice exceeding max licenses
        check_max_licenses_error(MAX_INVOICED_LICENSES + 1)
        with patch(
            "corporate.lib.stripe.get_latest_seat_count", return_value=MAX_INVOICED_LICENSES + 5
        ):
            check_max_licenses_error(MAX_INVOICED_LICENSES + 5)

        # Autopay with automatic license_management
        check_success(False, None)
        # Autopay with automatic license_management, should just ignore the licenses entry
        check_success(False, self.seat_count)
        # Autopay
        check_success(False, self.seat_count, {"license_management": "manual"})
        # Autopay has no limit on max licenses
        check_success(False, MAX_INVOICED_LICENSES + 1, {"license_management": "manual"})
        # Invoice
        check_success(True, self.seat_count + MIN_INVOICED_LICENSES)
        # Invoice
        check_success(True, MAX_INVOICED_LICENSES)

    def test_upgrade_with_uncaught_exception(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        with patch(
            "corporate.views.upgrade.process_initial_upgrade", side_effect=Exception
        ), self.assertLogs("corporate.stripe", "WARNING") as m:
            response = self.upgrade(talk_to_stripe=False)
            self.assertIn("ERROR:corporate.stripe:Uncaught exception in billing", m.output[0])
            self.assertIn(m.records[0].stack_info, m.output[0])
        self.assert_json_error_contains(
            response, "Something went wrong. Please contact desdemona+admin@zulip.com."
        )
        self.assertEqual(
            orjson.loads(response.content)["error_description"], "uncaught exception during upgrade"
        )

    def test_request_sponsorship_form_with_invalid_url(self) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        data = {
            "organization-type": Realm.ORG_TYPES["opensource"]["id"],
            "website": "invalid-url",
            "description": "Infinispan is a distributed in-memory key/value data store with optional schema.",
        }

        response = self.client_post("/json/billing/sponsorship", data)

        self.assert_json_error(response, "Enter a valid URL.")

    def test_request_sponsorship_form_with_blank_url(self) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        data = {
            "organization-type": Realm.ORG_TYPES["opensource"]["id"],
            "website": "",
            "description": "Infinispan is a distributed in-memory key/value data store with optional schema.",
        }

        response = self.client_post("/json/billing/sponsorship", data)

        self.assert_json_success(response)

    def test_request_sponsorship(self) -> None:
        user = self.example_user("hamlet")
        self.assertIsNone(get_customer_by_realm(user.realm))

        self.login_user(user)

        data = {
            "organization-type": Realm.ORG_TYPES["opensource"]["id"],
            "website": "https://infinispan.org/",
            "description": "Infinispan is a distributed in-memory key/value data store with optional schema.",
        }
        response = self.client_post("/json/billing/sponsorship", data)
        self.assert_json_success(response)

        sponsorship_request = ZulipSponsorshipRequest.objects.filter(
            realm=user.realm, requested_by=user
        ).first()
        assert sponsorship_request is not None
        self.assertEqual(sponsorship_request.org_website, data["website"])
        self.assertEqual(sponsorship_request.org_description, data["description"])
        self.assertEqual(
            sponsorship_request.org_type,
            Realm.ORG_TYPES["opensource"]["id"],
        )

        customer = get_customer_by_realm(user.realm)
        assert customer is not None
        self.assertEqual(customer.sponsorship_pending, True)
        from django.core.mail import outbox

        self.assert_length(outbox, 1)

        for message in outbox:
            self.assert_length(message.to, 1)
            self.assertEqual(message.to[0], "desdemona+admin@zulip.com")
            self.assertEqual(message.subject, "Sponsorship request (Open-source project) for zulip")
            self.assertEqual(message.reply_to, ["hamlet@zulip.com"])
            self.assertEqual(self.email_envelope_from(message), settings.NOREPLY_EMAIL_ADDRESS)
            self.assertIn("Zulip sponsorship <noreply-", self.email_display_from(message))
            self.assertIn("Requested by: King Hamlet (Member)", message.body)
            self.assertIn(
                "Support URL: http://zulip.testserver/activity/support?q=zulip", message.body
            )
            self.assertIn("Website: https://infinispan.org", message.body)
            self.assertIn("Organization type: Open-source", message.body)
            self.assertIn("Description:\nInfinispan is a distributed in-memory", message.body)

        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/billing/")

        response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["Your organization has requested sponsored or discounted hosting."], response
        )

        self.login_user(self.example_user("othello"))
        response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["You must be an organization owner or a billing administrator to view this page."],
            response,
        )

        user.realm.plan_type = Realm.STANDARD_FREE
        user.realm.save()
        self.login_user(self.example_user("hamlet"))
        response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["Your organization is fully sponsored and is on the <b>Zulip Standard</b>"], response
        )

    def test_redirect_for_billing_home(self) -> None:
        user = self.example_user("iago")
        self.login_user(user)
        response = self.client_get("/billing/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/upgrade/", response.url)

        user.realm.plan_type = Realm.STANDARD_FREE
        user.realm.save()
        response = self.client_get("/billing/")
        self.assertEqual(response.status_code, 200)

        user.realm.plan_type = Realm.LIMITED
        user.realm.save()
        Customer.objects.create(realm=user.realm, stripe_customer_id="cus_123")
        response = self.client_get("/billing/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual("/upgrade/", response.url)

    def test_redirect_for_upgrade_page(self) -> None:
        user = self.example_user("iago")
        self.login_user(user)

        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 200)

        user.realm.plan_type = Realm.STANDARD_FREE
        user.realm.save()
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/billing/")

        user.realm.plan_type = Realm.LIMITED
        user.realm.save()
        customer = Customer.objects.create(realm=user.realm, stripe_customer_id="cus_123")
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 200)

        CustomerPlan.objects.create(
            customer=customer,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=CustomerPlan.ANNUAL,
            tier=CustomerPlan.STANDARD,
        )
        response = self.client_get("/upgrade/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/billing/")

        with self.settings(FREE_TRIAL_DAYS=30):
            response = self.client_get("/upgrade/")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/billing/")

            response = self.client_get("/upgrade/", {"onboarding": "true"})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.url, "/billing/?onboarding=true")

    def test_get_latest_seat_count(self) -> None:
        realm = get_realm("zulip")
        initial_count = get_latest_seat_count(realm)
        user1 = UserProfile.objects.create(
            realm=realm, email="user1@zulip.com", delivery_email="user1@zulip.com"
        )
        user2 = UserProfile.objects.create(
            realm=realm, email="user2@zulip.com", delivery_email="user2@zulip.com"
        )
        self.assertEqual(get_latest_seat_count(realm), initial_count + 2)

        # Test that bots aren't counted
        user1.is_bot = True
        user1.save(update_fields=["is_bot"])
        self.assertEqual(get_latest_seat_count(realm), initial_count + 1)

        # Test that inactive users aren't counted
        do_deactivate_user(user2, acting_user=None)
        self.assertEqual(get_latest_seat_count(realm), initial_count)

        # Test guests
        # Adding a guest to a realm with a lot of members shouldn't change anything
        UserProfile.objects.create(
            realm=realm,
            email="user3@zulip.com",
            delivery_email="user3@zulip.com",
            role=UserProfile.ROLE_GUEST,
        )
        self.assertEqual(get_latest_seat_count(realm), initial_count)
        # Test 1 member and 5 guests
        realm = do_create_realm(string_id="second", name="second")
        UserProfile.objects.create(
            realm=realm, email="member@second.com", delivery_email="member@second.com"
        )
        for i in range(5):
            UserProfile.objects.create(
                realm=realm,
                email=f"guest{i}@second.com",
                delivery_email=f"guest{i}@second.com",
                role=UserProfile.ROLE_GUEST,
            )
        self.assertEqual(get_latest_seat_count(realm), 1)
        # Test 1 member and 6 guests
        UserProfile.objects.create(
            realm=realm,
            email="guest5@second.com",
            delivery_email="guest5@second.com",
            role=UserProfile.ROLE_GUEST,
        )
        self.assertEqual(get_latest_seat_count(realm), 2)

    def test_sign_string(self) -> None:
        string = "abc"
        signed_string, salt = sign_string(string)
        self.assertEqual(string, unsign_string(signed_string, salt))

        with self.assertRaises(signing.BadSignature):
            unsign_string(signed_string, "randomsalt")

    # This tests both the payment method string, and also is a very basic
    # test that the various upgrade paths involving non-standard payment
    # histories don't throw errors
    @mock_stripe()
    def test_payment_method_string(self, *mocks: Mock) -> None:
        pass
        # If you sign up with a card, we should show your card as the payment method
        # Already tested in test_initial_upgrade

        # If you pay by invoice, your payment method should be
        # "Billed by invoice", even if you have a card on file
        # user = self.example_user("hamlet")
        # do_create_stripe_customer(user, stripe_create_token().id)
        # self.login_user(user)
        # self.upgrade(invoice=True)
        # stripe_customer = stripe_get_customer(Customer.objects.get(realm=user.realm).stripe_customer_id)
        # self.assertEqual('Billed by invoice', payment_method_string(stripe_customer))

        # If you sign up with a card and then downgrade, we still have your
        # card on file, and should show it
        # TODO

    @mock_stripe()
    def test_attach_discount_to_realm(self, *mocks: Mock) -> None:
        # Attach discount before Stripe customer exists
        user = self.example_user("hamlet")
        attach_discount_to_realm(user.realm, Decimal(85), acting_user=user)
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_DISCOUNT_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = str({"old_discount": None, "new_discount": Decimal("85")})
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.login_user(user)
        # Check that the discount appears in page_params
        self.assert_in_success_response(["85"], self.client_get("/upgrade/"))
        # Check that the customer was charged the discounted amount
        self.upgrade()
        customer = Customer.objects.first()
        assert customer is not None
        [charge] = stripe.Charge.list(customer=customer.stripe_customer_id)
        self.assertEqual(1200 * self.seat_count, charge.amount)
        stripe_customer_id = customer.stripe_customer_id
        assert stripe_customer_id is not None
        [invoice] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual(
            [1200 * self.seat_count, -1200 * self.seat_count],
            [item.amount for item in invoice.lines],
        )
        # Check CustomerPlan reflects the discount
        plan = CustomerPlan.objects.get(price_per_license=1200, discount=Decimal(85))

        # Attach discount to existing Stripe customer
        plan.status = CustomerPlan.ENDED
        plan.save(update_fields=["status"])
        attach_discount_to_realm(user.realm, Decimal(25), acting_user=user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            process_initial_upgrade(
                user, self.seat_count, True, CustomerPlan.ANNUAL, stripe_create_token().id
            )
        [charge, _] = stripe.Charge.list(customer=customer.stripe_customer_id)
        self.assertEqual(6000 * self.seat_count, charge.amount)
        stripe_customer_id = customer.stripe_customer_id
        assert stripe_customer_id is not None
        [invoice, _] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual(
            [6000 * self.seat_count, -6000 * self.seat_count],
            [item.amount for item in invoice.lines],
        )
        plan = CustomerPlan.objects.get(price_per_license=6000, discount=Decimal(25))

        attach_discount_to_realm(user.realm, Decimal(50), acting_user=user)
        plan.refresh_from_db()
        self.assertEqual(plan.price_per_license, 4000)
        self.assertEqual(plan.discount, 50)
        customer.refresh_from_db()
        self.assertEqual(customer.default_discount, 50)
        invoice_plans_as_needed(self.next_year + timedelta(days=10))
        stripe_customer_id = customer.stripe_customer_id
        assert stripe_customer_id is not None
        [invoice, _, _] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual([4000 * self.seat_count], [item.amount for item in invoice.lines])
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_DISCOUNT_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = str(
            {"old_discount": Decimal("25.0000"), "new_discount": Decimal("50")}
        )
        self.assertEqual(realm_audit_log.extra_data, expected_extra_data)
        self.assertEqual(realm_audit_log.acting_user, user)

    def test_approve_sponsorship(self) -> None:
        user = self.example_user("hamlet")
        approve_sponsorship(user.realm, acting_user=user)
        realm = get_realm("zulip")
        self.assertEqual(realm.plan_type, Realm.STANDARD_FREE)

        expected_message = "Your organization's request for sponsored hosting has been approved! :tada:.\nYou have been upgraded to Zulip Cloud Standard, free of charge."
        sender = get_system_bot(settings.NOTIFICATION_BOT, user.realm_id)
        recipient_id = self.example_user("desdemona").recipient_id
        message = Message.objects.filter(sender=sender.id).first()
        assert message is not None
        self.assertEqual(message.content, expected_message)
        self.assertEqual(message.recipient.type, Recipient.PERSONAL)
        self.assertEqual(message.recipient_id, recipient_id)

    def test_update_sponsorship_status(self) -> None:
        lear = get_realm("lear")
        iago = self.example_user("iago")
        update_sponsorship_status(lear, True, acting_user=iago)
        customer = get_customer_by_realm(realm=lear)
        assert customer is not None
        self.assertTrue(customer.sponsorship_pending)
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_SPONSORSHIP_PENDING_STATUS_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"sponsorship_pending": True}
        self.assertEqual(realm_audit_log.extra_data, str(expected_extra_data))
        self.assertEqual(realm_audit_log.acting_user, iago)

    def test_get_discount_for_realm(self) -> None:
        user = self.example_user("hamlet")
        self.assertEqual(get_discount_for_realm(user.realm), None)

        attach_discount_to_realm(user.realm, Decimal(85), acting_user=None)
        self.assertEqual(get_discount_for_realm(user.realm), 85)

    @mock_stripe()
    def test_replace_payment_source(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        self.upgrade()
        # Create an open invoice
        stripe_customer = Customer.objects.first()
        assert stripe_customer is not None
        stripe_customer_id = stripe_customer.stripe_customer_id
        assert stripe_customer_id is not None
        stripe.InvoiceItem.create(amount=5000, currency="usd", customer=stripe_customer_id)
        stripe_invoice = stripe.Invoice.create(customer=stripe_customer_id)
        stripe.Invoice.finalize_invoice(stripe_invoice)
        RealmAuditLog.objects.filter(event_type=RealmAuditLog.STRIPE_CARD_CHANGED).delete()

        # Replace with an invalid card
        stripe_token = stripe_create_token(card_number="4000000000009987").id
        with patch("stripe.Invoice.list") as mock_invoice_list, self.assertLogs(
            "corporate.stripe", "INFO"
        ) as m:
            response = self.client_post(
                "/json/billing/sources/change",
                {"stripe_token": stripe_token},
            )
            self.assertEqual(
                m.output, ["INFO:corporate.stripe:Stripe card error: 402 card_error card_declined "]
            )
        mock_invoice_list.assert_not_called()
        self.assertEqual(orjson.loads(response.content)["error_description"], "card error")
        self.assert_json_error_contains(response, "Your card was declined")
        for stripe_source in stripe_get_customer(stripe_customer_id).sources:
            assert isinstance(stripe_source, stripe.Card)
            self.assertEqual(stripe_source.last4, "4242")
        self.assertFalse(
            RealmAuditLog.objects.filter(event_type=RealmAuditLog.STRIPE_CARD_CHANGED).exists()
        )

        # Replace with a card that's valid, but charging the card fails
        stripe_token = stripe_create_token(card_number="4000000000000341").id
        with self.assertLogs("corporate.stripe", "INFO") as m:
            response = self.client_post(
                "/json/billing/sources/change",
                {"stripe_token": stripe_token},
            )
            self.assertEqual(
                m.output,
                ["INFO:corporate.stripe:Stripe card error: 402 card_error card_declined None"],
            )
        self.assertEqual(orjson.loads(response.content)["error_description"], "card error")
        self.assert_json_error_contains(response, "Your card was declined")
        for stripe_source in stripe_get_customer(stripe_customer_id).sources:
            assert isinstance(stripe_source, stripe.Card)
            self.assertEqual(stripe_source.last4, "0341")
        self.assertEqual(
            len(list(stripe.Invoice.list(customer=stripe_customer_id, status="open"))), 1
        )
        self.assertEqual(
            1, RealmAuditLog.objects.filter(event_type=RealmAuditLog.STRIPE_CARD_CHANGED).count()
        )

        # Replace with a valid card
        stripe_token = stripe_create_token(card_number="5555555555554444").id
        response = self.client_post("/json/billing/sources/change", {"stripe_token": stripe_token})
        self.assert_json_success(response)
        number_of_sources = 0
        for stripe_source in stripe_get_customer(stripe_customer_id).sources:
            assert isinstance(stripe_source, stripe.Card)
            self.assertEqual(stripe_source.last4, "4444")
            number_of_sources += 1
        # Verify that we replaced the previous card, rather than adding a new one
        self.assertEqual(number_of_sources, 1)
        # Ideally we'd also test that we don't pay invoices with collection_method=='send_invoice'
        for stripe_invoice in stripe.Invoice.list(customer=stripe_customer_id):
            self.assertEqual(stripe_invoice.status, "paid")
        self.assertEqual(
            2, RealmAuditLog.objects.filter(event_type=RealmAuditLog.STRIPE_CARD_CHANGED).count()
        )

    def test_downgrade(self) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        plan = get_current_plan_by_realm(user.realm)
        assert plan is not None
        self.assertEqual(plan.licenses(), self.seat_count)
        self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count)
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}
                )
                stripe_customer_id = Customer.objects.get(realm=user.realm).id
                new_plan = get_current_plan_by_realm(user.realm)
                assert new_plan is not None
                expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}"
                self.assertEqual(m.output[0], expected_log)
                self.assert_json_success(response)
        plan.refresh_from_db()
        self.assertEqual(plan.licenses(), self.seat_count)
        self.assertEqual(plan.licenses_at_next_renewal(), None)

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            mock_customer = Mock(email=user.delivery_email, default_source=None)
            with patch(
                "corporate.views.billing_page.stripe_get_customer", return_value=mock_customer
            ):
                response = self.client_get("/billing/")
                self.assert_in_success_response(
                    [
                        "Your plan will be downgraded to <strong>Zulip Limited</strong> on "
                        "<strong>January 2, 2013</strong>",
                        "You plan is scheduled for downgrade on <strong>January 2, 2013</strong>",
                        "Cancel downgrade",
                    ],
                    response,
                )

        # Verify that we still write LicenseLedger rows during the remaining
        # part of the cycle
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=20):
            update_license_ledger_if_needed(user.realm, self.now)
        self.assertEqual(
            LicenseLedger.objects.order_by("-id")
            .values_list("licenses", "licenses_at_next_renewal")
            .first(),
            (20, 20),
        )

        # Verify that we invoice them for the additional users
        from stripe import Invoice

        Invoice.create = lambda **args: None  # type: ignore[assignment] # cleaner than mocking
        Invoice.finalize_invoice = lambda *args: None  # type: ignore[assignment] # cleaner than mocking
        with patch("stripe.InvoiceItem.create") as mocked:
            invoice_plans_as_needed(self.next_month)
        mocked.assert_called_once()
        mocked.reset_mock()

        # Check that we downgrade properly if the cycle is over
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=30):
            update_license_ledger_if_needed(user.realm, self.next_year)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(get_realm("zulip").plan_type, Realm.LIMITED)
        self.assertEqual(plan.status, CustomerPlan.ENDED)
        self.assertEqual(
            LicenseLedger.objects.order_by("-id")
            .values_list("licenses", "licenses_at_next_renewal")
            .first(),
            (20, 20),
        )

        # Verify that we don't write LicenseLedger rows once we've downgraded
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=40):
            update_license_ledger_if_needed(user.realm, self.next_year)
        self.assertEqual(
            LicenseLedger.objects.order_by("-id")
            .values_list("licenses", "licenses_at_next_renewal")
            .first(),
            (20, 20),
        )

        # Verify that we call invoice_plan once more after cycle end but
        # don't invoice them for users added after the cycle end
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertIsNotNone(plan.next_invoice_date)
        with patch("stripe.InvoiceItem.create") as mocked:
            invoice_plans_as_needed(self.next_year + timedelta(days=32))
        mocked.assert_not_called()
        mocked.reset_mock()
        # Check that we updated next_invoice_date in invoice_plan
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertIsNone(plan.next_invoice_date)

        # Check that we don't call invoice_plan after that final call
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=50):
            update_license_ledger_if_needed(user.realm, self.next_year + timedelta(days=80))
        with patch("corporate.lib.stripe.invoice_plan") as mocked:
            invoice_plans_as_needed(self.next_year + timedelta(days=400))
        mocked.assert_not_called()

    @mock_stripe()
    def test_switch_from_monthly_plan_to_annual_plan_for_automatic_license_management(
        self, *mocks: Mock
    ) -> None:
        user = self.example_user("hamlet")

        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade(schedule="monthly")
        monthly_plan = get_current_plan_by_realm(user.realm)
        assert monthly_plan is not None
        self.assertEqual(monthly_plan.automanage_licenses, True)
        self.assertEqual(monthly_plan.billing_schedule, CustomerPlan.MONTHLY)

        stripe_customer_id = Customer.objects.get(realm=user.realm).id
        new_plan = get_current_plan_by_realm(user.realm)
        assert new_plan is not None

        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_patch(
                    "/json/billing/plan",
                    {"status": CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE},
                )
                expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE}"
                self.assertEqual(m.output[0], expected_log)
                self.assert_json_success(response)
        monthly_plan.refresh_from_db()
        self.assertEqual(monthly_plan.status, CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE)
        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["be switched from monthly to annual billing on <strong>February 2, 2012"], response
        )

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=20):
            update_license_ledger_if_needed(user.realm, self.now)
        self.assertEqual(LicenseLedger.objects.filter(plan=monthly_plan).count(), 2)
        self.assertEqual(
            LicenseLedger.objects.order_by("-id")
            .values_list("licenses", "licenses_at_next_renewal")
            .first(),
            (20, 20),
        )

        with patch("corporate.lib.stripe.timezone_now", return_value=self.next_month):
            with patch("corporate.lib.stripe.get_latest_seat_count", return_value=25):
                update_license_ledger_if_needed(user.realm, self.next_month)
        self.assertEqual(LicenseLedger.objects.filter(plan=monthly_plan).count(), 2)
        customer = get_customer_by_realm(user.realm)
        assert customer is not None
        self.assertEqual(CustomerPlan.objects.filter(customer=customer).count(), 2)
        monthly_plan.refresh_from_db()
        self.assertEqual(monthly_plan.status, CustomerPlan.ENDED)
        self.assertEqual(monthly_plan.next_invoice_date, self.next_month)
        annual_plan = get_current_plan_by_realm(user.realm)
        assert annual_plan is not None
        self.assertEqual(annual_plan.status, CustomerPlan.ACTIVE)
        self.assertEqual(annual_plan.billing_schedule, CustomerPlan.ANNUAL)
        self.assertEqual(annual_plan.invoicing_status, CustomerPlan.INITIAL_INVOICE_TO_BE_SENT)
        self.assertEqual(annual_plan.billing_cycle_anchor, self.next_month)
        self.assertEqual(annual_plan.next_invoice_date, self.next_month)
        self.assertEqual(annual_plan.invoiced_through, None)
        annual_ledger_entries = LicenseLedger.objects.filter(plan=annual_plan).order_by("id")
        self.assert_length(annual_ledger_entries, 2)
        self.assertEqual(annual_ledger_entries[0].is_renewal, True)
        self.assertEqual(
            annual_ledger_entries.values_list("licenses", "licenses_at_next_renewal")[0], (20, 20)
        )
        self.assertEqual(annual_ledger_entries[1].is_renewal, False)
        self.assertEqual(
            annual_ledger_entries.values_list("licenses", "licenses_at_next_renewal")[1], (25, 25)
        )
        audit_log = RealmAuditLog.objects.get(
            event_type=RealmAuditLog.CUSTOMER_SWITCHED_FROM_MONTHLY_TO_ANNUAL_PLAN
        )
        extra_data: str = assert_is_not_none(audit_log.extra_data)
        self.assertEqual(audit_log.realm, user.realm)
        self.assertEqual(orjson.loads(extra_data)["monthly_plan_id"], monthly_plan.id)
        self.assertEqual(orjson.loads(extra_data)["annual_plan_id"], annual_plan.id)

        invoice_plans_as_needed(self.next_month)

        annual_ledger_entries = LicenseLedger.objects.filter(plan=annual_plan).order_by("id")
        self.assert_length(annual_ledger_entries, 2)
        annual_plan.refresh_from_db()
        self.assertEqual(annual_plan.invoicing_status, CustomerPlan.DONE)
        self.assertEqual(annual_plan.invoiced_through, annual_ledger_entries[1])
        self.assertEqual(annual_plan.billing_cycle_anchor, self.next_month)
        self.assertEqual(annual_plan.next_invoice_date, add_months(self.next_month, 1))
        monthly_plan.refresh_from_db()
        self.assertEqual(monthly_plan.next_invoice_date, None)

        assert customer.stripe_customer_id
        [invoice0, invoice1, invoice2] = stripe.Invoice.list(customer=customer.stripe_customer_id)

        [invoice_item0, invoice_item1] = invoice0.get("lines")
        annual_plan_invoice_item_params = {
            "amount": 5 * 80 * 100,
            "description": "Additional license (Feb 2, 2012 - Feb 2, 2013)",
            "plan": None,
            "quantity": 5,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.next_month),
                "end": datetime_to_timestamp(add_months(self.next_month, 12)),
            },
        }
        for key, value in annual_plan_invoice_item_params.items():
            self.assertEqual(invoice_item0[key], value)

        annual_plan_invoice_item_params = {
            "amount": 20 * 80 * 100,
            "description": "Zulip Standard - renewal",
            "plan": None,
            "quantity": 20,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.next_month),
                "end": datetime_to_timestamp(add_months(self.next_month, 12)),
            },
        }
        for key, value in annual_plan_invoice_item_params.items():
            self.assertEqual(invoice_item1[key], value)

        [monthly_plan_invoice_item] = invoice1.get("lines")
        monthly_plan_invoice_item_params = {
            "amount": 14 * 8 * 100,
            "description": "Additional license (Jan 2, 2012 - Feb 2, 2012)",
            "plan": None,
            "quantity": 14,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.now),
                "end": datetime_to_timestamp(self.next_month),
            },
        }
        for key, value in monthly_plan_invoice_item_params.items():
            self.assertEqual(monthly_plan_invoice_item[key], value)

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=30):
            update_license_ledger_if_needed(user.realm, add_months(self.next_month, 1))
        invoice_plans_as_needed(add_months(self.next_month, 1))

        [invoice0, invoice1, invoice2, invoice3] = stripe.Invoice.list(
            customer=customer.stripe_customer_id
        )

        [monthly_plan_invoice_item] = invoice0.get("lines")
        monthly_plan_invoice_item_params = {
            "amount": 5 * 7366,
            "description": "Additional license (Mar 2, 2012 - Feb 2, 2013)",
            "plan": None,
            "quantity": 5,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(add_months(self.next_month, 1)),
                "end": datetime_to_timestamp(add_months(self.next_month, 12)),
            },
        }
        for key, value in monthly_plan_invoice_item_params.items():
            self.assertEqual(monthly_plan_invoice_item[key], value)
        invoice_plans_as_needed(add_months(self.now, 13))

        [invoice0, invoice1, invoice2, invoice3, invoice4] = stripe.Invoice.list(
            customer=customer.stripe_customer_id
        )

        [invoice_item] = invoice0.get("lines")
        annual_plan_invoice_item_params = {
            "amount": 30 * 80 * 100,
            "description": "Zulip Standard - renewal",
            "plan": None,
            "quantity": 30,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(add_months(self.next_month, 12)),
                "end": datetime_to_timestamp(add_months(self.next_month, 24)),
            },
        }
        for key, value in annual_plan_invoice_item_params.items():
            self.assertEqual(invoice_item[key], value)

    @mock_stripe()
    def test_switch_from_monthly_plan_to_annual_plan_for_manual_license_management(
        self, *mocks: Mock
    ) -> None:
        user = self.example_user("hamlet")
        num_licenses = 35

        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade(schedule="monthly", license_management="manual", licenses=num_licenses)
        monthly_plan = get_current_plan_by_realm(user.realm)
        assert monthly_plan is not None
        self.assertEqual(monthly_plan.automanage_licenses, False)
        self.assertEqual(monthly_plan.billing_schedule, CustomerPlan.MONTHLY)
        stripe_customer_id = Customer.objects.get(realm=user.realm).id
        new_plan = get_current_plan_by_realm(user.realm)
        assert new_plan is not None
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_patch(
                    "/json/billing/plan",
                    {"status": CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE},
                )
                self.assertEqual(
                    m.output[0],
                    f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE}",
                )
                self.assert_json_success(response)
        monthly_plan.refresh_from_db()
        self.assertEqual(monthly_plan.status, CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE)
        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            response = self.client_get("/billing/")
        self.assert_in_success_response(
            ["be switched from monthly to annual billing on <strong>February 2, 2012"], response
        )

        invoice_plans_as_needed(self.next_month)

        self.assertEqual(LicenseLedger.objects.filter(plan=monthly_plan).count(), 1)
        customer = get_customer_by_realm(user.realm)
        assert customer is not None
        self.assertEqual(CustomerPlan.objects.filter(customer=customer).count(), 2)
        monthly_plan.refresh_from_db()
        self.assertEqual(monthly_plan.status, CustomerPlan.ENDED)
        self.assertEqual(monthly_plan.next_invoice_date, None)
        annual_plan = get_current_plan_by_realm(user.realm)
        assert annual_plan is not None
        self.assertEqual(annual_plan.status, CustomerPlan.ACTIVE)
        self.assertEqual(annual_plan.billing_schedule, CustomerPlan.ANNUAL)
        self.assertEqual(annual_plan.invoicing_status, CustomerPlan.INITIAL_INVOICE_TO_BE_SENT)
        self.assertEqual(annual_plan.billing_cycle_anchor, self.next_month)
        self.assertEqual(annual_plan.next_invoice_date, self.next_month)
        annual_ledger_entries = LicenseLedger.objects.filter(plan=annual_plan).order_by("id")
        self.assert_length(annual_ledger_entries, 1)
        self.assertEqual(annual_ledger_entries[0].is_renewal, True)
        self.assertEqual(
            annual_ledger_entries.values_list("licenses", "licenses_at_next_renewal")[0],
            (num_licenses, num_licenses),
        )
        self.assertEqual(annual_plan.invoiced_through, None)

        # First call of invoice_plans_as_needed creates the new plan. Second call
        # calls invoice_plan on the newly created plan.
        invoice_plans_as_needed(self.next_month + timedelta(days=1))

        annual_plan.refresh_from_db()
        self.assertEqual(annual_plan.invoiced_through, annual_ledger_entries[0])
        self.assertEqual(annual_plan.next_invoice_date, add_months(self.next_month, 12))
        self.assertEqual(annual_plan.invoicing_status, CustomerPlan.DONE)

        assert customer.stripe_customer_id
        [invoice0, invoice1] = stripe.Invoice.list(customer=customer.stripe_customer_id)

        [invoice_item] = invoice0.get("lines")
        annual_plan_invoice_item_params = {
            "amount": num_licenses * 80 * 100,
            "description": "Zulip Standard - renewal",
            "plan": None,
            "quantity": num_licenses,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.next_month),
                "end": datetime_to_timestamp(add_months(self.next_month, 12)),
            },
        }
        for key, value in annual_plan_invoice_item_params.items():
            self.assertEqual(invoice_item[key], value)

        with patch("corporate.lib.stripe.invoice_plan") as m:
            invoice_plans_as_needed(add_months(self.now, 2))
            m.assert_not_called()

        invoice_plans_as_needed(add_months(self.now, 13))

        [invoice0, invoice1, invoice2] = stripe.Invoice.list(customer=customer.stripe_customer_id)

        [invoice_item] = invoice0.get("lines")
        annual_plan_invoice_item_params = {
            "amount": num_licenses * 80 * 100,
            "description": "Zulip Standard - renewal",
            "plan": None,
            "quantity": num_licenses,
            "subscription": None,
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(add_months(self.next_month, 12)),
                "end": datetime_to_timestamp(add_months(self.next_month, 24)),
            },
        }
        for key, value in annual_plan_invoice_item_params.items():
            self.assertEqual(invoice_item[key], value)

    def test_reupgrade_after_plan_status_changed_to_downgrade_at_end_of_cycle(self) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}
                )
                stripe_customer_id = Customer.objects.get(realm=user.realm).id
                new_plan = get_current_plan_by_realm(user.realm)
                assert new_plan is not None
                expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}"
                self.assertEqual(m.output[0], expected_log)
                self.assert_json_success(response)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.status, CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE)
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                response = self.client_patch("/json/billing/plan", {"status": CustomerPlan.ACTIVE})
                expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.ACTIVE}"
                self.assertEqual(m.output[0], expected_log)
                self.assert_json_success(response)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.status, CustomerPlan.ACTIVE)

    @patch("stripe.Invoice.create")
    @patch("stripe.Invoice.finalize_invoice")
    @patch("stripe.InvoiceItem.create")
    def test_downgrade_during_invoicing(self, *mocks: Mock) -> None:
        # The difference between this test and test_downgrade is that
        # CustomerPlan.status is DOWNGRADE_AT_END_OF_CYCLE rather than ENDED
        # when we call invoice_plans_as_needed
        # This test is essentially checking that we call make_end_of_cycle_updates_if_needed
        # during the invoicing process.
        user = self.example_user("hamlet")
        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        with self.assertLogs("corporate.stripe", "INFO") as m:
            stripe_customer_id = Customer.objects.get(realm=user.realm).id
            new_plan = get_current_plan_by_realm(user.realm)
            assert new_plan is not None
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}
                )
            expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}"
            self.assertEqual(m.output[0], expected_log)

        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertIsNotNone(plan.next_invoice_date)
        self.assertEqual(plan.status, CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE)
        invoice_plans_as_needed(self.next_year)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertIsNone(plan.next_invoice_date)
        self.assertEqual(plan.status, CustomerPlan.ENDED)

    def test_downgrade_free_trial(self) -> None:
        user = self.example_user("hamlet")

        free_trial_end_date = self.now + timedelta(days=60)
        with self.settings(FREE_TRIAL_DAYS=60):
            with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
                self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

            plan = CustomerPlan.objects.get()
            self.assertEqual(plan.next_invoice_date, free_trial_end_date)
            self.assertEqual(get_realm("zulip").plan_type, Realm.STANDARD)
            self.assertEqual(plan.status, CustomerPlan.FREE_TRIAL)

            # Add some extra users before the realm is deactivated
            with patch("corporate.lib.stripe.get_latest_seat_count", return_value=21):
                update_license_ledger_if_needed(user.realm, self.now)

            last_ledger_entry = LicenseLedger.objects.order_by("id").last()
            assert last_ledger_entry is not None
            self.assertEqual(last_ledger_entry.licenses, 21)
            self.assertEqual(last_ledger_entry.licenses_at_next_renewal, 21)

            self.login_user(user)

            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                self.client_patch("/json/billing/plan", {"status": CustomerPlan.ENDED})

            plan.refresh_from_db()
            self.assertEqual(get_realm("zulip").plan_type, Realm.LIMITED)
            self.assertEqual(plan.status, CustomerPlan.ENDED)
            self.assertEqual(plan.invoiced_through, last_ledger_entry)
            self.assertIsNone(plan.next_invoice_date)

            self.login_user(user)
            response = self.client_get("/billing/")
            self.assert_in_success_response(
                ["Your organization is on the <b>Zulip Free</b>"], response
            )

            # The extra users added in the final month are not charged
            with patch("corporate.lib.stripe.invoice_plan") as mocked:
                invoice_plans_as_needed(self.next_month)
            mocked.assert_not_called()

            # The plan is not renewed after an year
            with patch("corporate.lib.stripe.invoice_plan") as mocked:
                invoice_plans_as_needed(self.next_year)
            mocked.assert_not_called()

    def test_reupgrade_by_billing_admin_after_downgrade(self) -> None:
        user = self.example_user("hamlet")

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        self.login_user(user)
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}
                )
            stripe_customer_id = Customer.objects.get(realm=user.realm).id
            new_plan = get_current_plan_by_realm(user.realm)
            assert new_plan is not None
            expected_log = f"INFO:corporate.stripe:Change plan status: Customer.id: {stripe_customer_id}, CustomerPlan.id: {new_plan.id}, status: {CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}"
            self.assertEqual(m.output[0], expected_log)

        with self.assertRaises(BillingError) as context, self.assertLogs(
            "corporate.stripe", "WARNING"
        ) as m:
            with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
                self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        self.assertEqual(
            m.output[0],
            f"WARNING:corporate.stripe:Customer <Customer <Realm: zulip {user.realm.id}> id> trying to upgrade, but has an active subscription",
        )
        self.assertEqual(
            context.exception.error_description, "subscribing with existing subscription"
        )

        invoice_plans_as_needed(self.next_year)

        response = self.client_get("/billing/")
        self.assert_in_success_response(["Your organization is on the <b>Zulip Free</b>"], response)

        with patch("corporate.lib.stripe.timezone_now", return_value=self.next_year):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(CustomerPlan.objects.count(), 2)

        current_plan = CustomerPlan.objects.all().order_by("id").last()
        assert current_plan is not None
        next_invoice_date = add_months(self.next_year, 1)
        self.assertEqual(current_plan.next_invoice_date, next_invoice_date)
        self.assertEqual(get_realm("zulip").plan_type, Realm.STANDARD)
        self.assertEqual(current_plan.status, CustomerPlan.ACTIVE)

        old_plan = CustomerPlan.objects.all().order_by("id").first()
        assert old_plan is not None
        self.assertEqual(old_plan.next_invoice_date, None)
        self.assertEqual(old_plan.status, CustomerPlan.ENDED)

    @mock_stripe()
    def test_update_licenses_of_manual_plan_from_billing_page(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade(invoice=True, licenses=100)

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses": 100})
            self.assert_json_error_contains(
                result, "Your plan is already on 100 licenses in the current billing period."
            )

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses_at_next_renewal": 100})
            self.assert_json_error_contains(
                result, "Your plan is already scheduled to renew with 100 licenses."
            )

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses": 50})
            self.assert_json_error_contains(
                result, "You cannot decrease the licenses in the current billing period."
            )

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses_at_next_renewal": 25})
            self.assert_json_error_contains(result, "You must invoice for at least 30 users.")

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses": 2000})
            self.assert_json_error_contains(
                result, "Invoices with more than 1000 licenses can't be processed from this page."
            )

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses": 150})
            self.assert_json_success(result)
        invoice_plans_as_needed(self.next_year)
        stripe_customer = stripe_get_customer(
            assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
        )
        [invoice, _] = stripe.Invoice.list(customer=stripe_customer.id)
        invoice_params = {
            "amount_due": (8000 * 150 + 8000 * 50),
            "amount_paid": 0,
            "attempt_count": 0,
            "auto_advance": True,
            "collection_method": "send_invoice",
            "statement_descriptor": "Zulip Standard",
            "status": "open",
            "total": (8000 * 150 + 8000 * 50),
        }
        for key, value in invoice_params.items():
            self.assertEqual(invoice.get(key), value)
        [renewal_item, extra_license_item] = invoice.lines
        line_item_params = {
            "amount": 8000 * 150,
            "description": "Zulip Standard - renewal",
            "discountable": False,
            "period": {
                "end": datetime_to_timestamp(self.next_year + timedelta(days=365)),
                "start": datetime_to_timestamp(self.next_year),
            },
            "plan": None,
            "proration": False,
            "quantity": 150,
        }
        for key, value in line_item_params.items():
            self.assertEqual(renewal_item.get(key), value)
        line_item_params = {
            "amount": 8000 * 50,
            "description": "Additional license (Jan 2, 2012 - Jan 2, 2013)",
            "discountable": False,
            "period": {
                "end": datetime_to_timestamp(self.next_year),
                "start": datetime_to_timestamp(self.now),
            },
            "plan": None,
            "proration": False,
            "quantity": 50,
        }
        for key, value in line_item_params.items():
            self.assertEqual(extra_license_item.get(key), value)

        with patch("corporate.views.billing_page.timezone_now", return_value=self.next_year):
            result = self.client_patch("/json/billing/plan", {"licenses_at_next_renewal": 120})
            self.assert_json_success(result)
        invoice_plans_as_needed(self.next_year + timedelta(days=365))
        stripe_customer = stripe_get_customer(
            assert_is_not_none(Customer.objects.get(realm=user.realm).stripe_customer_id)
        )
        [invoice, _, _] = stripe.Invoice.list(customer=stripe_customer.id)
        invoice_params = {
            "amount_due": 8000 * 120,
            "amount_paid": 0,
            "attempt_count": 0,
            "auto_advance": True,
            "collection_method": "send_invoice",
            "statement_descriptor": "Zulip Standard",
            "status": "open",
            "total": 8000 * 120,
        }
        for key, value in invoice_params.items():
            self.assertEqual(invoice.get(key), value)
        [renewal_item] = invoice.lines
        line_item_params = {
            "amount": 8000 * 120,
            "description": "Zulip Standard - renewal",
            "discountable": False,
            "period": {
                "end": datetime_to_timestamp(self.next_year + timedelta(days=2 * 365)),
                "start": datetime_to_timestamp(self.next_year + timedelta(days=365)),
            },
            "plan": None,
            "proration": False,
            "quantity": 120,
        }
        for key, value in line_item_params.items():
            self.assertEqual(renewal_item.get(key), value)

    def test_update_licenses_of_automatic_plan_from_billing_page(self) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses": 100})
            self.assert_json_error_contains(result, "Your plan is on automatic license management.")

        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            result = self.client_patch("/json/billing/plan", {"licenses_at_next_renewal": 100})
            self.assert_json_error_contains(result, "Your plan is on automatic license management.")

    def test_update_plan_with_invalid_status(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        self.login_user(self.example_user("hamlet"))

        response = self.client_patch(
            "/json/billing/plan",
            {"status": CustomerPlan.NEVER_STARTED},
        )
        self.assert_json_error_contains(response, "Invalid status")

    def test_update_plan_without_any_params(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        self.login_user(self.example_user("hamlet"))
        with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
            response = self.client_patch("/json/billing/plan", {})
        self.assert_json_error_contains(response, "Nothing to change")

    def test_update_plan_that_which_is_due_for_expiry(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        self.login_user(self.example_user("hamlet"))
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                result = self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE}
                )
                self.assert_json_success(result)
                self.assertRegex(
                    m.output[0],
                    r"INFO:corporate.stripe:Change plan status: Customer.id: \d*, CustomerPlan.id: \d*, status: 2",
                )

        with patch("corporate.lib.stripe.timezone_now", return_value=self.next_year):
            result = self.client_patch("/json/billing/plan", {"status": CustomerPlan.ACTIVE})
            self.assert_json_error_contains(
                result, "Unable to update the plan. The plan has ended."
            )

    def test_update_plan_that_which_is_due_for_replacement(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.MONTHLY, "token")

        self.login_user(self.example_user("hamlet"))
        with self.assertLogs("corporate.stripe", "INFO") as m:
            with patch("corporate.views.billing_page.timezone_now", return_value=self.now):
                result = self.client_patch(
                    "/json/billing/plan", {"status": CustomerPlan.SWITCH_TO_ANNUAL_AT_END_OF_CYCLE}
                )
                self.assert_json_success(result)
                self.assertRegex(
                    m.output[0],
                    r"INFO:corporate.stripe:Change plan status: Customer.id: \d*, CustomerPlan.id: \d*, status: 4",
                )

        with patch("corporate.lib.stripe.timezone_now", return_value=self.next_month):
            result = self.client_patch("/json/billing/plan", {})
            self.assert_json_error_contains(
                result,
                "Unable to update the plan. The plan has been expired and replaced with a new plan.",
            )

    @patch("corporate.lib.stripe.billing_logger.info")
    def test_deactivate_realm(self, mock_: Mock) -> None:
        user = self.example_user("hamlet")
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        plan = CustomerPlan.objects.get()
        self.assertEqual(plan.next_invoice_date, self.next_month)
        self.assertEqual(get_realm("zulip").plan_type, Realm.STANDARD)
        self.assertEqual(plan.status, CustomerPlan.ACTIVE)

        # Add some extra users before the realm is deactivated
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=20):
            update_license_ledger_if_needed(user.realm, self.now)

        last_ledger_entry = LicenseLedger.objects.order_by("id").last()
        assert last_ledger_entry is not None
        self.assertEqual(last_ledger_entry.licenses, 20)
        self.assertEqual(last_ledger_entry.licenses_at_next_renewal, 20)

        do_deactivate_realm(get_realm("zulip"), acting_user=None)

        plan.refresh_from_db()
        self.assertTrue(get_realm("zulip").deactivated)
        self.assertEqual(get_realm("zulip").plan_type, Realm.LIMITED)
        self.assertEqual(plan.status, CustomerPlan.ENDED)
        self.assertEqual(plan.invoiced_through, last_ledger_entry)
        self.assertIsNone(plan.next_invoice_date)

        do_reactivate_realm(get_realm("zulip"))

        self.login_user(user)
        response = self.client_get("/billing/")
        self.assert_in_success_response(["Your organization is on the <b>Zulip Free</b>"], response)

        # The extra users added in the final month are not charged
        with patch("corporate.lib.stripe.invoice_plan") as mocked:
            invoice_plans_as_needed(self.next_month)
        mocked.assert_not_called()

        # The plan is not renewed after an year
        with patch("corporate.lib.stripe.invoice_plan") as mocked:
            invoice_plans_as_needed(self.next_year)
        mocked.assert_not_called()

    def test_reupgrade_by_billing_admin_after_realm_deactivation(self) -> None:
        user = self.example_user("hamlet")

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        do_deactivate_realm(get_realm("zulip"), acting_user=None)
        self.assertTrue(get_realm("zulip").deactivated)
        do_reactivate_realm(get_realm("zulip"))

        self.login_user(user)
        response = self.client_get("/billing/")
        self.assert_in_success_response(["Your organization is on the <b>Zulip Free</b>"], response)

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")

        self.assertEqual(Customer.objects.count(), 1)

        self.assertEqual(CustomerPlan.objects.count(), 2)

        current_plan = CustomerPlan.objects.all().order_by("id").last()
        assert current_plan is not None
        self.assertEqual(current_plan.next_invoice_date, self.next_month)
        self.assertEqual(get_realm("zulip").plan_type, Realm.STANDARD)
        self.assertEqual(current_plan.status, CustomerPlan.ACTIVE)

        old_plan = CustomerPlan.objects.all().order_by("id").first()
        assert old_plan is not None
        self.assertEqual(old_plan.next_invoice_date, None)
        self.assertEqual(old_plan.status, CustomerPlan.ENDED)

    @mock_stripe()
    def test_void_all_open_invoices(self, *mock: Mock) -> None:
        iago = self.example_user("iago")
        king = self.lear_user("king")

        self.assertEqual(void_all_open_invoices(iago.realm), 0)

        zulip_customer = update_or_create_stripe_customer(iago)
        lear_customer = update_or_create_stripe_customer(king)

        assert zulip_customer.stripe_customer_id
        stripe.InvoiceItem.create(
            currency="usd",
            customer=zulip_customer.stripe_customer_id,
            description="Zulip standard upgrade",
            discountable=False,
            unit_amount=800,
            quantity=8,
        )
        stripe_invoice = stripe.Invoice.create(
            auto_advance=True,
            collection_method="send_invoice",
            customer=zulip_customer.stripe_customer_id,
            days_until_due=30,
            statement_descriptor="Zulip Standard",
        )
        stripe.Invoice.finalize_invoice(stripe_invoice)

        assert lear_customer.stripe_customer_id
        stripe.InvoiceItem.create(
            currency="usd",
            customer=lear_customer.stripe_customer_id,
            description="Zulip standard upgrade",
            discountable=False,
            unit_amount=800,
            quantity=8,
        )
        stripe_invoice = stripe.Invoice.create(
            auto_advance=True,
            collection_method="send_invoice",
            customer=lear_customer.stripe_customer_id,
            days_until_due=30,
            statement_descriptor="Zulip Standard",
        )
        stripe.Invoice.finalize_invoice(stripe_invoice)

        self.assertEqual(void_all_open_invoices(iago.realm), 1)
        invoices = stripe.Invoice.list(customer=zulip_customer.stripe_customer_id)
        self.assert_length(invoices, 1)
        for invoice in invoices:
            self.assertEqual(invoice.status, "void")

        lear_stripe_customer_id = lear_customer.stripe_customer_id
        lear_customer.stripe_customer_id = None
        lear_customer.save(update_fields=["stripe_customer_id"])
        self.assertEqual(void_all_open_invoices(king.realm), 0)

        lear_customer.stripe_customer_id = lear_stripe_customer_id
        lear_customer.save(update_fields=["stripe_customer_id"])
        self.assertEqual(void_all_open_invoices(king.realm), 1)
        invoices = stripe.Invoice.list(customer=lear_customer.stripe_customer_id)
        self.assert_length(invoices, 1)
        for invoice in invoices:
            self.assertEqual(invoice.status, "void")

    def create_invoices(self, customer: Customer, num_invoices: int) -> List[stripe.Invoice]:
        invoices = []
        assert customer.stripe_customer_id is not None
        for _ in range(num_invoices):
            stripe.InvoiceItem.create(
                amount=10000,
                currency="usd",
                customer=customer.stripe_customer_id,
                description="Zulip standard",
                discountable=False,
            )
            invoice = stripe.Invoice.create(
                auto_advance=True,
                collection_method="send_invoice",
                customer=customer.stripe_customer_id,
                days_until_due=DEFAULT_INVOICE_DAYS_UNTIL_DUE,
                statement_descriptor="Zulip Standard",
            )
            stripe.Invoice.finalize_invoice(invoice)
            invoices.append(invoice)
        return invoices

    @mock_stripe()
    def test_downgrade_small_realms_behind_on_payments_as_needed(self, *mock: Mock) -> None:
        def create_realm(
            users_to_create: int,
            create_stripe_customer: bool,
            create_plan: bool,
            num_invoices: Optional[int] = None,
        ) -> Tuple[Realm, Optional[Customer], Optional[CustomerPlan], List[stripe.Invoice]]:
            realm_string_id = "realm_" + str(random.randrange(1, 1000000))
            realm = Realm.objects.create(string_id=realm_string_id)
            users = []
            for i in range(users_to_create):
                user = UserProfile.objects.create(
                    delivery_email=f"user-{i}-{realm_string_id}@zulip.com",
                    email=f"user-{i}-{realm_string_id}@zulip.com",
                    realm=realm,
                )
                users.append(user)

            customer = None
            if create_stripe_customer:
                customer = do_create_stripe_customer(users[0])
            plan = None
            if create_plan:
                plan, _ = self.subscribe_realm_to_monthly_plan_on_manual_license_management(
                    realm, users_to_create, users_to_create
                )
            invoices = []
            if num_invoices is not None:
                assert customer is not None
                invoices = self.create_invoices(customer, num_invoices)
            return realm, customer, plan, invoices

        @dataclass
        class Row:
            realm: Realm
            expected_plan_type: int
            plan: Optional[CustomerPlan]
            expected_plan_status: Optional[int]
            void_all_open_invoices_mock_called: bool
            email_expected_to_be_sent: bool

        rows: List[Row] = []
        realm, _, _, _ = create_realm(
            users_to_create=1, create_stripe_customer=False, create_plan=False
        )
        # To create local Customer object but no Stripe customer.
        attach_discount_to_realm(realm, Decimal(20), acting_user=None)
        rows.append(Row(realm, Realm.SELF_HOSTED, None, None, False, False))

        realm, _, _, _ = create_realm(
            users_to_create=1, create_stripe_customer=True, create_plan=False
        )
        rows.append(Row(realm, Realm.SELF_HOSTED, None, None, False, False))

        realm, customer, _, _ = create_realm(
            users_to_create=1, create_stripe_customer=True, create_plan=False, num_invoices=1
        )
        rows.append(Row(realm, Realm.SELF_HOSTED, None, None, True, False))

        realm, _, plan, _ = create_realm(
            users_to_create=1, create_stripe_customer=True, create_plan=True
        )
        rows.append(Row(realm, Realm.STANDARD, plan, CustomerPlan.ACTIVE, False, False))

        realm, customer, plan, _ = create_realm(
            users_to_create=1, create_stripe_customer=True, create_plan=True, num_invoices=1
        )
        rows.append(Row(realm, Realm.STANDARD, plan, CustomerPlan.ACTIVE, False, False))

        realm, customer, plan, _ = create_realm(
            users_to_create=3, create_stripe_customer=True, create_plan=True, num_invoices=2
        )
        rows.append(Row(realm, Realm.LIMITED, plan, CustomerPlan.ENDED, True, True))

        realm, customer, plan, invoices = create_realm(
            users_to_create=1, create_stripe_customer=True, create_plan=True, num_invoices=2
        )
        for invoice in invoices:
            stripe.Invoice.pay(invoice, paid_out_of_band=True)
        rows.append(Row(realm, Realm.STANDARD, plan, CustomerPlan.ACTIVE, False, False))

        realm, customer, plan, _ = create_realm(
            users_to_create=20, create_stripe_customer=True, create_plan=True, num_invoices=2
        )
        rows.append(Row(realm, Realm.STANDARD, plan, CustomerPlan.ACTIVE, False, False))

        with patch("corporate.lib.stripe.void_all_open_invoices") as void_all_open_invoices_mock:
            downgrade_small_realms_behind_on_payments_as_needed()

        from django.core.mail import outbox

        for row in rows:
            row.realm.refresh_from_db()
            self.assertEqual(row.realm.plan_type, row.expected_plan_type)
            if row.plan is not None:
                row.plan.refresh_from_db()
                self.assertEqual(row.plan.status, row.expected_plan_status)
            if row.void_all_open_invoices_mock_called:
                void_all_open_invoices_mock.assert_any_call(row.realm)
            else:
                try:
                    void_all_open_invoices_mock.assert_any_call(row.realm)
                except AssertionError:
                    pass
                else:  # nocoverage
                    raise AssertionError("void_all_open_invoices_mock should not be called")

            email_found = False
            for email in outbox:
                recipient = UserProfile.objects.get(email=email.to[0])
                if recipient.realm == row.realm:
                    self.assertIn(
                        f"Your organization, http://{row.realm.string_id}.testserver, has been downgraded",
                        outbox[0].body,
                    )
                    self.assert_length(email.to, 1)
                    self.assertTrue(recipient.is_billing_admin)
                    email_found = True
            self.assertEqual(row.email_expected_to_be_sent, email_found)

    def test_update_billing_method_of_current_plan(self) -> None:
        realm = get_realm("zulip")
        customer = Customer.objects.create(realm=realm, stripe_customer_id="cus_12345")
        plan = CustomerPlan.objects.create(
            customer=customer,
            status=CustomerPlan.ACTIVE,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=CustomerPlan.ANNUAL,
            tier=CustomerPlan.STANDARD,
        )
        self.assertEqual(plan.charge_automatically, False)

        iago = self.example_user("iago")
        update_billing_method_of_current_plan(realm, True, acting_user=iago)
        plan.refresh_from_db()
        self.assertEqual(plan.charge_automatically, True)
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_BILLING_METHOD_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"charge_automatically": plan.charge_automatically}
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm_audit_log.extra_data, str(expected_extra_data))

        update_billing_method_of_current_plan(realm, False, acting_user=iago)
        plan.refresh_from_db()
        self.assertEqual(plan.charge_automatically, False)
        realm_audit_log = RealmAuditLog.objects.filter(
            event_type=RealmAuditLog.REALM_BILLING_METHOD_CHANGED
        ).last()
        assert realm_audit_log is not None
        expected_extra_data = {"charge_automatically": plan.charge_automatically}
        self.assertEqual(realm_audit_log.acting_user, iago)
        self.assertEqual(realm_audit_log.extra_data, str(expected_extra_data))

    @mock_stripe()
    def test_customer_has_credit_card_as_default_source(self, *mocks: Mock) -> None:
        iago = self.example_user("iago")
        customer = Customer.objects.create(realm=iago.realm)
        self.assertFalse(customer_has_credit_card_as_default_source(customer))

        customer = do_create_stripe_customer(iago)
        self.assertFalse(customer_has_credit_card_as_default_source(customer))

        customer = do_create_stripe_customer(iago, stripe_token=stripe_create_token().id)
        self.assertTrue(customer_has_credit_card_as_default_source(customer))


class RequiresBillingAccessTest(ZulipTestCase):
    def setUp(self) -> None:
        super().setUp()
        hamlet = self.example_user("hamlet")
        hamlet.is_billing_admin = True
        hamlet.save(update_fields=["is_billing_admin"])

        desdemona = self.example_user("desdemona")
        desdemona.role = UserProfile.ROLE_REALM_OWNER
        desdemona.save(update_fields=["role"])

    def test_who_can_access_json_endpoints(self) -> None:
        # Billing admins have access
        self.login_user(self.example_user("hamlet"))
        with patch("corporate.views.billing_page.do_replace_payment_source") as mocked1:
            response = self.client_post("/json/billing/sources/change", {"stripe_token": "token"})
        self.assert_json_success(response)
        mocked1.assert_called_once()

        # Realm owners have access, even if they are not billing admins
        self.login_user(self.example_user("desdemona"))
        with patch("corporate.views.billing_page.do_replace_payment_source") as mocked2:
            response = self.client_post("/json/billing/sources/change", {"stripe_token": "token"})
        self.assert_json_success(response)
        mocked2.assert_called_once()

    def test_who_cant_access_json_endpoints(self) -> None:
        def verify_user_cant_access_endpoint(
            username: str,
            endpoint: str,
            method: str,
            request_data: Dict[str, Union[str, int]],
            error_message: str,
        ) -> None:

            self.login_user(self.example_user(username))
            if method == "POST":
                response = self.client_post(endpoint, request_data)
            elif method == "PATCH":
                response = self.client_patch(endpoint, request_data)
            else:
                raise AssertionError("Invalid method")
            self.assert_json_error_contains(response, error_message)

        verify_user_cant_access_endpoint(
            "polonius",
            "/json/billing/upgrade",
            "POST",
            {
                "billing_modality": "charge_automatically",
                "schedule": "annual",
                "signed_seat_count": "signed count",
                "salt": "salt",
            },
            "Must be an organization member",
        )

        verify_user_cant_access_endpoint(
            "polonius",
            "/json/billing/sponsorship",
            "POST",
            {
                "organization-type": "event",
                "description": "event description",
                "website": "example.com",
            },
            "Must be an organization member",
        )

        for username in ["cordelia", "iago"]:
            self.login_user(self.example_user(username))
            verify_user_cant_access_endpoint(
                username,
                "/json/billing/sources/change",
                "POST",
                {"stripe_token": "token"},
                "Must be a billing administrator or an organization owner",
            )

            verify_user_cant_access_endpoint(
                username,
                "/json/billing/plan",
                "PATCH",
                {"status": 1},
                "Must be a billing administrator or an organization owner",
            )

        # Make sure that we are testing all the JSON endpoints
        # Quite a hack, but probably fine for now
        string_with_all_endpoints = str(get_resolver("corporate.urls").reverse_dict)
        json_endpoints = {
            word.strip("\"'()[],$") for word in string_with_all_endpoints.split() if "json/" in word
        }
        self.assert_length(json_endpoints, 4)


class BillingHelpersTest(ZulipTestCase):
    def test_next_month(self) -> None:
        anchor = datetime(2019, 12, 31, 1, 2, 3, tzinfo=timezone.utc)
        period_boundaries = [
            anchor,
            datetime(2020, 1, 31, 1, 2, 3, tzinfo=timezone.utc),
            # Test that this is the 28th even during leap years
            datetime(2020, 2, 28, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 3, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 4, 30, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 5, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 6, 30, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 7, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 8, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 9, 30, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 10, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 11, 30, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2020, 12, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2021, 1, 31, 1, 2, 3, tzinfo=timezone.utc),
            datetime(2021, 2, 28, 1, 2, 3, tzinfo=timezone.utc),
        ]
        with self.assertRaises(AssertionError):
            add_months(anchor, -1)
        # Explicitly test add_months for each value of MAX_DAY_FOR_MONTH and
        # for crossing a year boundary
        for i, boundary in enumerate(period_boundaries):
            self.assertEqual(add_months(anchor, i), boundary)
        # Test next_month for small values
        for last, next_ in zip(period_boundaries[:-1], period_boundaries[1:]):
            self.assertEqual(next_month(anchor, last), next_)
        # Test next_month for large values
        period_boundaries = [dt.replace(year=dt.year + 100) for dt in period_boundaries]
        for last, next_ in zip(period_boundaries[:-1], period_boundaries[1:]):
            self.assertEqual(next_month(anchor, last), next_)

    def test_compute_plan_parameters(self) -> None:
        # TODO: test rounding down microseconds
        anchor = datetime(2019, 12, 31, 1, 2, 3, tzinfo=timezone.utc)
        month_later = datetime(2020, 1, 31, 1, 2, 3, tzinfo=timezone.utc)
        year_later = datetime(2020, 12, 31, 1, 2, 3, tzinfo=timezone.utc)
        test_cases = [
            # test all possibilities, since there aren't that many
            ((True, CustomerPlan.ANNUAL, None), (anchor, month_later, year_later, 8000)),
            ((True, CustomerPlan.ANNUAL, 85), (anchor, month_later, year_later, 1200)),
            ((True, CustomerPlan.MONTHLY, None), (anchor, month_later, month_later, 800)),
            ((True, CustomerPlan.MONTHLY, 85), (anchor, month_later, month_later, 120)),
            ((False, CustomerPlan.ANNUAL, None), (anchor, year_later, year_later, 8000)),
            ((False, CustomerPlan.ANNUAL, 85), (anchor, year_later, year_later, 1200)),
            ((False, CustomerPlan.MONTHLY, None), (anchor, month_later, month_later, 800)),
            ((False, CustomerPlan.MONTHLY, 85), (anchor, month_later, month_later, 120)),
            # test exact math of Decimals; 800 * (1 - 87.25) = 101.9999999..
            ((False, CustomerPlan.MONTHLY, 87.25), (anchor, month_later, month_later, 102)),
            # test dropping of fractional cents; without the int it's 102.8
            ((False, CustomerPlan.MONTHLY, 87.15), (anchor, month_later, month_later, 102)),
        ]
        with patch("corporate.lib.stripe.timezone_now", return_value=anchor):
            for (automanage_licenses, discount, free_trial), output in test_cases:
                output_ = compute_plan_parameters(
                    automanage_licenses,
                    discount,
                    None if free_trial is None else Decimal(free_trial),
                )
                self.assertEqual(output_, output)

    def test_get_price_per_license(self) -> None:
        self.assertEqual(get_price_per_license(CustomerPlan.STANDARD, CustomerPlan.ANNUAL), 8000)
        self.assertEqual(get_price_per_license(CustomerPlan.STANDARD, CustomerPlan.MONTHLY), 800)
        self.assertEqual(
            get_price_per_license(
                CustomerPlan.STANDARD, CustomerPlan.MONTHLY, discount=Decimal(50)
            ),
            400,
        )

        with self.assertRaises(AssertionError):
            get_price_per_license(CustomerPlan.PLUS, CustomerPlan.MONTHLY)

        with self.assertRaisesRegex(InvalidBillingSchedule, "Unknown billing_schedule: 1000"):
            get_price_per_license(CustomerPlan.STANDARD, 1000)

    def test_update_or_create_stripe_customer_logic(self) -> None:
        user = self.example_user("hamlet")
        # No existing Customer object
        with patch(
            "corporate.lib.stripe.do_create_stripe_customer", return_value="returned"
        ) as mocked1:
            returned = update_or_create_stripe_customer(user, stripe_token="token")
        mocked1.assert_called_once()
        self.assertEqual(returned, "returned")

        customer = Customer.objects.create(realm=get_realm("zulip"))
        # Customer exists but stripe_customer_id is None
        with patch(
            "corporate.lib.stripe.do_create_stripe_customer", return_value="returned"
        ) as mocked2:
            returned = update_or_create_stripe_customer(user, stripe_token="token")
        mocked2.assert_called_once()
        self.assertEqual(returned, "returned")

        customer.stripe_customer_id = "cus_12345"
        customer.save()
        # Customer exists, replace payment source
        with patch("corporate.lib.stripe.do_replace_payment_source") as mocked3:
            returned_customer = update_or_create_stripe_customer(
                self.example_user("hamlet"), "token"
            )
        mocked3.assert_called_once()
        self.assertEqual(returned_customer, customer)

        # Customer exists, do nothing
        with patch("corporate.lib.stripe.do_replace_payment_source") as mocked4:
            returned_customer = update_or_create_stripe_customer(self.example_user("hamlet"), None)
        mocked4.assert_not_called()
        self.assertEqual(returned_customer, customer)

    def test_get_customer_by_realm(self) -> None:
        realm = get_realm("zulip")

        self.assertEqual(get_customer_by_realm(realm), None)

        customer = Customer.objects.create(realm=realm, stripe_customer_id="cus_12345")
        self.assertEqual(get_customer_by_realm(realm), customer)

    def test_get_current_plan_by_customer(self) -> None:
        realm = get_realm("zulip")
        customer = Customer.objects.create(realm=realm, stripe_customer_id="cus_12345")

        self.assertEqual(get_current_plan_by_customer(customer), None)

        plan = CustomerPlan.objects.create(
            customer=customer,
            status=CustomerPlan.ACTIVE,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=CustomerPlan.ANNUAL,
            tier=CustomerPlan.STANDARD,
        )
        self.assertEqual(get_current_plan_by_customer(customer), plan)

        plan.status = CustomerPlan.DOWNGRADE_AT_END_OF_CYCLE
        plan.save(update_fields=["status"])
        self.assertEqual(get_current_plan_by_customer(customer), plan)

        plan.status = CustomerPlan.ENDED
        plan.save(update_fields=["status"])
        self.assertEqual(get_current_plan_by_customer(customer), None)

        plan.status = CustomerPlan.NEVER_STARTED
        plan.save(update_fields=["status"])
        self.assertEqual(get_current_plan_by_customer(customer), None)

    def test_get_current_plan_by_realm(self) -> None:
        realm = get_realm("zulip")

        self.assertEqual(get_current_plan_by_realm(realm), None)

        customer = Customer.objects.create(realm=realm, stripe_customer_id="cus_12345")
        self.assertEqual(get_current_plan_by_realm(realm), None)

        plan = CustomerPlan.objects.create(
            customer=customer,
            status=CustomerPlan.ACTIVE,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=CustomerPlan.ANNUAL,
            tier=CustomerPlan.STANDARD,
        )
        self.assertEqual(get_current_plan_by_realm(realm), plan)

    def test_get_realms_to_default_discount_dict(self) -> None:
        Customer.objects.create(realm=get_realm("zulip"), stripe_customer_id="cus_1")
        lear_customer = Customer.objects.create(realm=get_realm("lear"), stripe_customer_id="cus_2")
        lear_customer.default_discount = Decimal(30)
        lear_customer.save(update_fields=["default_discount"])
        zephyr_customer = Customer.objects.create(
            realm=get_realm("zephyr"), stripe_customer_id="cus_3"
        )
        zephyr_customer.default_discount = Decimal(0)
        zephyr_customer.save(update_fields=["default_discount"])

        self.assertEqual(
            get_realms_to_default_discount_dict(),
            {
                "lear": Decimal("30.0000"),
            },
        )

    def test_is_realm_on_free_trial(self) -> None:
        realm = get_realm("zulip")
        self.assertFalse(is_realm_on_free_trial(realm))

        customer = Customer.objects.create(realm=realm, stripe_customer_id="cus_12345")
        plan = CustomerPlan.objects.create(
            customer=customer,
            status=CustomerPlan.ACTIVE,
            billing_cycle_anchor=timezone_now(),
            billing_schedule=CustomerPlan.ANNUAL,
            tier=CustomerPlan.STANDARD,
        )
        self.assertFalse(is_realm_on_free_trial(realm))

        plan.status = CustomerPlan.FREE_TRIAL
        plan.save(update_fields=["status"])
        self.assertTrue(is_realm_on_free_trial(realm))

    def test_is_sponsored_realm(self) -> None:
        realm = get_realm("zulip")
        self.assertFalse(is_sponsored_realm(realm))

        realm.plan_type = Realm.STANDARD_FREE
        realm.save()
        self.assertTrue(is_sponsored_realm(realm))


class LicenseLedgerTest(StripeTestCase):
    def test_add_plan_renewal_if_needed(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        self.assertEqual(LicenseLedger.objects.count(), 1)
        plan = CustomerPlan.objects.get()
        # Plan hasn't renewed yet
        make_end_of_cycle_updates_if_needed(plan, self.next_year - timedelta(days=1))
        self.assertEqual(LicenseLedger.objects.count(), 1)
        # Plan needs to renew
        # TODO: do_deactivate_user for a user, so that licenses_at_next_renewal != licenses
        new_plan, ledger_entry = make_end_of_cycle_updates_if_needed(plan, self.next_year)
        self.assertIsNone(new_plan)
        self.assertEqual(LicenseLedger.objects.count(), 2)
        ledger_params = {
            "plan": plan,
            "is_renewal": True,
            "event_time": self.next_year,
            "licenses": self.seat_count,
            "licenses_at_next_renewal": self.seat_count,
        }
        for key, value in ledger_params.items():
            self.assertEqual(getattr(ledger_entry, key), value)
        # Plan needs to renew, but we already added the plan_renewal ledger entry
        make_end_of_cycle_updates_if_needed(plan, self.next_year + timedelta(days=1))
        self.assertEqual(LicenseLedger.objects.count(), 2)

    def test_update_license_ledger_if_needed(self) -> None:
        realm = get_realm("zulip")
        # Test no Customer
        update_license_ledger_if_needed(realm, self.now)
        self.assertFalse(LicenseLedger.objects.exists())
        # Test plan not automanaged
        self.local_upgrade(self.seat_count + 1, False, CustomerPlan.ANNUAL, "token")
        plan = CustomerPlan.objects.get()
        self.assertEqual(LicenseLedger.objects.count(), 1)
        self.assertEqual(plan.licenses(), self.seat_count + 1)
        self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count + 1)
        update_license_ledger_if_needed(realm, self.now)
        self.assertEqual(LicenseLedger.objects.count(), 1)
        # Test no active plan
        plan.automanage_licenses = True
        plan.status = CustomerPlan.ENDED
        plan.save(update_fields=["automanage_licenses", "status"])
        update_license_ledger_if_needed(realm, self.now)
        self.assertEqual(LicenseLedger.objects.count(), 1)
        # Test update needed
        plan.status = CustomerPlan.ACTIVE
        plan.save(update_fields=["status"])
        update_license_ledger_if_needed(realm, self.now)
        self.assertEqual(LicenseLedger.objects.count(), 2)

    def test_update_license_ledger_for_automanaged_plan(self) -> None:
        realm = get_realm("zulip")
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.licenses(), self.seat_count)
        self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count)
        # Simple increase
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=23):
            update_license_ledger_for_automanaged_plan(realm, plan, self.now)
            self.assertEqual(plan.licenses(), 23)
            self.assertEqual(plan.licenses_at_next_renewal(), 23)
        # Decrease
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=20):
            update_license_ledger_for_automanaged_plan(realm, plan, self.now)
            self.assertEqual(plan.licenses(), 23)
            self.assertEqual(plan.licenses_at_next_renewal(), 20)
        # Increase, but not past high watermark
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=21):
            update_license_ledger_for_automanaged_plan(realm, plan, self.now)
            self.assertEqual(plan.licenses(), 23)
            self.assertEqual(plan.licenses_at_next_renewal(), 21)
        # Increase, but after renewal date, and below last year's high watermark
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=22):
            update_license_ledger_for_automanaged_plan(
                realm, plan, self.next_year + timedelta(seconds=1)
            )
            self.assertEqual(plan.licenses(), 22)
            self.assertEqual(plan.licenses_at_next_renewal(), 22)

        ledger_entries = list(
            LicenseLedger.objects.values_list(
                "is_renewal", "event_time", "licenses", "licenses_at_next_renewal"
            ).order_by("id")
        )
        self.assertEqual(
            ledger_entries,
            [
                (True, self.now, self.seat_count, self.seat_count),
                (False, self.now, 23, 23),
                (False, self.now, 23, 20),
                (False, self.now, 23, 21),
                (True, self.next_year, 21, 21),
                (False, self.next_year + timedelta(seconds=1), 22, 22),
            ],
        )

    def test_update_license_ledger_for_manual_plan(self) -> None:
        realm = get_realm("zulip")

        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count + 1, False, CustomerPlan.ANNUAL, "token")

        plan = get_current_plan_by_realm(realm)
        assert plan is not None

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            update_license_ledger_for_manual_plan(plan, self.now, licenses=self.seat_count + 3)
            self.assertEqual(plan.licenses(), self.seat_count + 3)
            self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count + 3)

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            with self.assertRaises(AssertionError):
                update_license_ledger_for_manual_plan(plan, self.now, licenses=self.seat_count)

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            update_license_ledger_for_manual_plan(
                plan, self.now, licenses_at_next_renewal=self.seat_count
            )
            self.assertEqual(plan.licenses(), self.seat_count + 3)
            self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count)

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            with self.assertRaises(AssertionError):
                update_license_ledger_for_manual_plan(
                    plan, self.now, licenses_at_next_renewal=self.seat_count - 1
                )

        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            update_license_ledger_for_manual_plan(plan, self.now, licenses=self.seat_count + 10)
            self.assertEqual(plan.licenses(), self.seat_count + 10)
            self.assertEqual(plan.licenses_at_next_renewal(), self.seat_count + 10)

        make_end_of_cycle_updates_if_needed(plan, self.next_year)
        self.assertEqual(plan.licenses(), self.seat_count + 10)

        ledger_entries = list(
            LicenseLedger.objects.values_list(
                "is_renewal", "event_time", "licenses", "licenses_at_next_renewal"
            ).order_by("id")
        )

        self.assertEqual(
            ledger_entries,
            [
                (True, self.now, self.seat_count + 1, self.seat_count + 1),
                (False, self.now, self.seat_count + 3, self.seat_count + 3),
                (False, self.now, self.seat_count + 3, self.seat_count),
                (False, self.now, self.seat_count + 10, self.seat_count + 10),
                (True, self.next_year, self.seat_count + 10, self.seat_count + 10),
            ],
        )

        with self.assertRaises(AssertionError):
            update_license_ledger_for_manual_plan(plan, self.now)

    def test_user_changes(self) -> None:
        self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        user = do_create_user("email", "password", get_realm("zulip"), "name", acting_user=None)
        do_deactivate_user(user, acting_user=None)
        do_reactivate_user(user, acting_user=None)
        # Not a proper use of do_activate_mirror_dummy_user, but fine for this test
        do_activate_mirror_dummy_user(user, acting_user=None)
        ledger_entries = list(
            LicenseLedger.objects.values_list(
                "is_renewal", "licenses", "licenses_at_next_renewal"
            ).order_by("id")
        )
        self.assertEqual(
            ledger_entries,
            [
                (True, self.seat_count, self.seat_count),
                (False, self.seat_count + 1, self.seat_count + 1),
                (False, self.seat_count + 1, self.seat_count),
                (False, self.seat_count + 1, self.seat_count + 1),
                (False, self.seat_count + 1, self.seat_count + 1),
            ],
        )


class InvoiceTest(StripeTestCase):
    def test_invoicing_status_is_started(self) -> None:
        self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        plan = CustomerPlan.objects.first()
        assert plan is not None
        plan.invoicing_status = CustomerPlan.STARTED
        plan.save(update_fields=["invoicing_status"])
        with self.assertRaises(NotImplementedError):
            invoice_plan(assert_is_not_none(CustomerPlan.objects.first()), self.now)

    def test_invoice_plan_without_stripe_customer(self) -> None:
        self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL)
        plan = get_current_plan_by_realm(get_realm("zulip"))
        assert plan and plan.customer
        plan.customer.stripe_customer_id = None
        plan.customer.save(update_fields=["stripe_customer_id"])
        with self.assertRaises(BillingError) as context:
            invoice_plan(plan, timezone_now())
        self.assertRegex(
            context.exception.error_description,
            "Realm zulip has a paid plan without a Stripe customer",
        )

    @mock_stripe()
    def test_invoice_plan(self, *mocks: Mock) -> None:
        user = self.example_user("hamlet")
        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade()
        # Increase
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count + 3):
            update_license_ledger_if_needed(get_realm("zulip"), self.now + timedelta(days=100))
        # Decrease
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count):
            update_license_ledger_if_needed(get_realm("zulip"), self.now + timedelta(days=200))
        # Increase, but not past high watermark
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count + 1):
            update_license_ledger_if_needed(get_realm("zulip"), self.now + timedelta(days=300))
        # Increase, but after renewal date, and below last year's high watermark
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count + 2):
            update_license_ledger_if_needed(get_realm("zulip"), self.now + timedelta(days=400))
        # Increase, but after event_time
        with patch("corporate.lib.stripe.get_latest_seat_count", return_value=self.seat_count + 3):
            update_license_ledger_if_needed(get_realm("zulip"), self.now + timedelta(days=500))
        plan = CustomerPlan.objects.first()
        assert plan is not None
        invoice_plan(plan, self.now + timedelta(days=400))
        stripe_cutomer_id = plan.customer.stripe_customer_id
        assert stripe_cutomer_id is not None
        [invoice0, invoice1] = stripe.Invoice.list(customer=stripe_cutomer_id)
        self.assertIsNotNone(invoice0.status_transitions.finalized_at)
        [item0, item1, item2] = invoice0.lines
        line_item_params = {
            "amount": int(8000 * (1 - ((400 - 366) / 365)) + 0.5),
            "description": "Additional license (Feb 5, 2013 - Jan 2, 2014)",
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.now + timedelta(days=400)),
                "end": datetime_to_timestamp(self.now + timedelta(days=2 * 365 + 1)),
            },
            "quantity": 1,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item0.get(key), value)
        line_item_params = {
            "amount": 8000 * (self.seat_count + 1),
            "description": "Zulip Standard - renewal",
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.now + timedelta(days=366)),
                "end": datetime_to_timestamp(self.now + timedelta(days=2 * 365 + 1)),
            },
            "quantity": (self.seat_count + 1),
        }
        for key, value in line_item_params.items():
            self.assertEqual(item1.get(key), value)
        line_item_params = {
            "amount": 3 * int(8000 * (366 - 100) / 366 + 0.5),
            "description": "Additional license (Apr 11, 2012 - Jan 2, 2013)",
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.now + timedelta(days=100)),
                "end": datetime_to_timestamp(self.now + timedelta(days=366)),
            },
            "quantity": 3,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item2.get(key), value)

    @mock_stripe()
    def test_fixed_price_plans(self, *mocks: Mock) -> None:
        # Also tests charge_automatically=False
        user = self.example_user("hamlet")
        self.login_user(user)
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.upgrade(invoice=True)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        plan.fixed_price = 100
        plan.price_per_license = 0
        plan.save(update_fields=["fixed_price", "price_per_license"])
        invoice_plan(plan, self.next_year)
        stripe_customer_id = plan.customer.stripe_customer_id
        assert stripe_customer_id is not None
        [invoice0, invoice1] = stripe.Invoice.list(customer=stripe_customer_id)
        self.assertEqual(invoice0.collection_method, "send_invoice")
        [item] = invoice0.lines
        line_item_params = {
            "amount": 100,
            "description": "Zulip Standard - renewal",
            "discountable": False,
            "period": {
                "start": datetime_to_timestamp(self.next_year),
                "end": datetime_to_timestamp(self.next_year + timedelta(days=365)),
            },
            "quantity": 1,
        }
        for key, value in line_item_params.items():
            self.assertEqual(item.get(key), value)

    def test_no_invoice_needed(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.next_invoice_date, self.next_month)
        # Test this doesn't make any calls to stripe.Invoice or stripe.InvoiceItem
        invoice_plan(plan, self.next_month)
        plan = CustomerPlan.objects.first()
        # Test that we still update next_invoice_date
        assert plan is not None
        self.assertEqual(plan.next_invoice_date, self.next_month + timedelta(days=29))

    def test_invoice_plans_as_needed(self) -> None:
        with patch("corporate.lib.stripe.timezone_now", return_value=self.now):
            self.local_upgrade(self.seat_count, True, CustomerPlan.ANNUAL, "token")
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.next_invoice_date, self.next_month)
        # Test nothing needed to be done
        with patch("corporate.lib.stripe.invoice_plan") as mocked:
            invoice_plans_as_needed(self.next_month - timedelta(days=1))
        mocked.assert_not_called()
        # Test something needing to be done
        invoice_plans_as_needed(self.next_month)
        plan = CustomerPlan.objects.first()
        assert plan is not None
        self.assertEqual(plan.next_invoice_date, self.next_month + timedelta(days=29))


class TestTestClasses(ZulipTestCase):
    def test_subscribe_realm_to_manual_license_management_plan(self) -> None:
        realm = get_realm("zulip")
        plan, ledger = self.subscribe_realm_to_manual_license_management_plan(
            realm, 50, 60, CustomerPlan.ANNUAL
        )

        plan.refresh_from_db()
        self.assertEqual(plan.automanage_licenses, False)
        self.assertEqual(plan.billing_schedule, CustomerPlan.ANNUAL)
        self.assertEqual(plan.tier, CustomerPlan.STANDARD)
        self.assertEqual(plan.licenses(), 50)
        self.assertEqual(plan.licenses_at_next_renewal(), 60)

        ledger.refresh_from_db()
        self.assertEqual(ledger.plan, plan)
        self.assertEqual(ledger.licenses, 50)
        self.assertEqual(ledger.licenses_at_next_renewal, 60)

        realm.refresh_from_db()
        self.assertEqual(realm.plan_type, Realm.STANDARD)

    def test_subscribe_realm_to_monthly_plan_on_manual_license_management(self) -> None:
        realm = get_realm("zulip")
        plan, ledger = self.subscribe_realm_to_monthly_plan_on_manual_license_management(
            realm, 20, 30
        )

        plan.refresh_from_db()
        self.assertEqual(plan.automanage_licenses, False)
        self.assertEqual(plan.billing_schedule, CustomerPlan.MONTHLY)
        self.assertEqual(plan.tier, CustomerPlan.STANDARD)
        self.assertEqual(plan.licenses(), 20)
        self.assertEqual(plan.licenses_at_next_renewal(), 30)

        ledger.refresh_from_db()
        self.assertEqual(ledger.plan, plan)
        self.assertEqual(ledger.licenses, 20)
        self.assertEqual(ledger.licenses_at_next_renewal, 30)

        realm.refresh_from_db()
        self.assertEqual(realm.plan_type, Realm.STANDARD)
