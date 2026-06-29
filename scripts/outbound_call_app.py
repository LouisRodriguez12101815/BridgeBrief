from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from twilio.rest import Client
from twilio.twiml.voice_response import Dial, VoiceResponse


DEFAULT_CALLER_ID = "+14075536936"
DEFAULT_VOICE = "alice"
STATUS_CALLBACK_EVENTS = ["initiated", "ringing", "answered", "completed"]


def load_environment() -> None:
    env_path = Path(__file__).with_name(".env")
    load_dotenv(env_path)


def normalize_phone_number(value: str, field_name: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if value.startswith("+") and len(digits) >= 10:
        return value
    raise ValueError(f"{field_name} must be a valid US-format phone number.")


def build_intro(args: argparse.Namespace) -> str:
    if args.intro:
        return args.intro

    pieces = ["Connecting your Aloha Systems call"]
    if args.lead_name:
        pieces.append(f"to {args.lead_name}")
    if args.company:
        pieces.append(f"at {args.company}")
    return " ".join(pieces) + "."


def build_twiml(args: argparse.Namespace) -> str:
    response = VoiceResponse()
    response.say(build_intro(args), voice=args.voice)

    dial_kwargs = {
        "caller_id": args.caller_id,
        "answer_on_bridge": True,
        "timeout": args.timeout,
    }
    if args.record:
        dial_kwargs["record"] = "record-from-answer"

    dial = Dial(**dial_kwargs)
    dial.number(args.lead_number)
    response.append(dial)
    response.say(
        "The called party did not answer. Please review the lead and try again later.",
        voice=args.voice,
    )
    return str(response)


def create_client() -> Client:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be available in the environment or .env file."
        )
    return Client(account_sid, auth_token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a live outbound call bridge using Twilio. Twilio calls you first, "
            "then connects you to the lead from the dedicated Aloha caller ID."
        )
    )
    parser.add_argument(
        "--agent-number",
        default=os.getenv("TWILIO_BRIDGE_AGENT_NUMBER"),
        help="Phone number that Twilio should call first so you can join the live conversation.",
    )
    parser.add_argument(
        "--lead-number",
        required=True,
        help="Destination lead phone number that will be dialed after you answer.",
    )
    parser.add_argument(
        "--lead-name",
        default="",
        help="Lead name used in the local pre-call summary.",
    )
    parser.add_argument(
        "--company",
        default="",
        help="Company name used in the local pre-call summary.",
    )
    parser.add_argument(
        "--caller-id",
        default=os.getenv("ALOHA_OUTBOUND_CALLER_ID", DEFAULT_CALLER_ID),
        help="Twilio number to display to the lead. Defaults to the dedicated Aloha outbound number.",
    )
    parser.add_argument(
        "--intro",
        default="",
        help="Optional message read to you when the bridge call starts.",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help="Twilio voice used for the short bridge prompt.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="How many seconds the lead leg rings before Twilio stops trying.",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Record the live call after the lead answers.",
    )
    parser.add_argument(
        "--status-callback",
        default=os.getenv("TWILIO_CALL_STATUS_CALLBACK", ""),
        help="Optional webhook for outbound call status events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated TwiML and resolved call details without placing a call.",
    )
    return parser.parse_args()


def print_summary(args: argparse.Namespace, twiml: str) -> None:
    print("Outbound Call Summary")
    print(f"  Agent number: {args.agent_number}")
    print(f"  Lead number:  {args.lead_number}")
    print(f"  Caller ID:    {args.caller_id}")
    if args.lead_name:
        print(f"  Lead name:    {args.lead_name}")
    if args.company:
        print(f"  Company:      {args.company}")
    print("")
    print("Generated TwiML")
    print(twiml)


def main() -> int:
    load_environment()
    args = parse_args()

    if not args.agent_number:
        print(
            "An agent callback number is required. Pass --agent-number or define TWILIO_BRIDGE_AGENT_NUMBER.",
            file=sys.stderr,
        )
        return 1

    try:
        args.agent_number = normalize_phone_number(args.agent_number, "agent number")
        args.lead_number = normalize_phone_number(args.lead_number, "lead number")
        args.caller_id = normalize_phone_number(args.caller_id, "caller ID")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    twiml = build_twiml(args)

    if args.dry_run:
        print_summary(args, twiml)
        return 0

    client = create_client()
    create_kwargs = {
        "to": args.agent_number,
        "from_": args.caller_id,
        "twiml": twiml,
    }
    if args.status_callback:
        create_kwargs["status_callback"] = args.status_callback
        create_kwargs["status_callback_event"] = STATUS_CALLBACK_EVENTS

    call = client.calls.create(**create_kwargs)

    print("Outbound call started.")
    print(f"  Call SID:   {call.sid}")
    print(f"  Agent leg:  {args.agent_number}")
    print(f"  Lead leg:   {args.lead_number}")
    print(f"  Caller ID:  {args.caller_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
