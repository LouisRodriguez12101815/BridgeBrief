from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from twilio.base.exceptions import TwilioRestException

from outbound_call_app import (
    DEFAULT_CALLER_ID,
    DEFAULT_VOICE,
    build_twiml,
    create_client,
    load_environment,
    normalize_phone_number,
)


DEFAULT_CSV_PATH = Path(__file__).with_name("bridgebrief-demo-leads.csv")
DEFAULT_RESULTS_DIR = Path(__file__).with_name("call_results")
DEFAULT_RECORDINGS_DIR = Path(__file__).with_name("recordings")
DEFAULT_POLL_SECONDS = 5
DEFAULT_MAX_WAIT_SECONDS = 900
DEFAULT_RECORDING_WAIT_SECONDS = 120
TERMINAL_CALL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


@dataclass
class Lead:
    row_number: int
    id: str
    phone_number: str
    company_name: str
    contact_name: str
    email: str
    city: str
    state: str
    zip_code: str
    industry: str
    line_type: str
    source: str
    created_at: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a small sequential outbound calling batch from a CSV lead file. "
            "Twilio calls you first, then bridges each lead from the dedicated Aloha 407 number."
        )
    )
    parser.add_argument(
        "--csv-path",
        default=str(DEFAULT_CSV_PATH),
        help="Path to the lead CSV file.",
    )
    parser.add_argument(
        "--agent-number",
        default=os.getenv("TWILIO_BRIDGE_AGENT_NUMBER"),
        help="Phone number that Twilio should call first so you can join the live conversation.",
    )
    parser.add_argument(
        "--caller-id",
        default=os.getenv("ALOHA_OUTBOUND_CALLER_ID", DEFAULT_CALLER_ID),
        help="Twilio number to display to the lead. Defaults to the dedicated Aloha 407 number.",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help="Twilio voice used for the bridge prompt.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="How many seconds each lead leg rings before Twilio stops trying.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=DEFAULT_POLL_SECONDS,
        help="How often to poll Twilio for call completion.",
    )
    parser.add_argument(
        "--max-wait-seconds",
        type=int,
        default=DEFAULT_MAX_WAIT_SECONDS,
        help="Maximum time to wait for each call to finish before recording a timeout result.",
    )
    parser.add_argument(
        "--recording-wait-seconds",
        type=int,
        default=DEFAULT_RECORDING_WAIT_SECONDS,
        help="Maximum time to wait for Twilio to finish processing each recording after the call ends.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of leads to attempt. Defaults to all selected leads.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based lead position to start from after filtering.",
    )
    parser.add_argument(
        "--only-line-types",
        default="",
        help="Optional comma-separated line types to include, such as mobile,landline.",
    )
    parser.add_argument(
        "--results-path",
        default="",
        help="Optional output CSV path for call results. Defaults to a timestamped file in call_results.",
    )
    parser.add_argument(
        "--recordings-dir",
        default="",
        help="Optional directory to download completed recordings into. Defaults to a timestamped folder in recordings.",
    )
    parser.add_argument(
        "--status-callback",
        default=os.getenv("TWILIO_CALL_STATUS_CALLBACK", ""),
        help="Optional Twilio status callback URL.",
    )
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="Continue automatically to the next lead instead of prompting after each call.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected leads and generated paths without placing calls.",
    )
    return parser.parse_args()


def load_leads(csv_path: Path) -> list[Lead]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        leads: list[Lead] = []
        for index, row in enumerate(reader, start=1):
            leads.append(
                Lead(
                    row_number=index,
                    id=(row.get("id") or "").strip(),
                    phone_number=(row.get("phone_number") or "").strip(),
                    company_name=(row.get("company_name") or "").strip(),
                    contact_name=(row.get("contact_name") or "").strip(),
                    email=(row.get("email") or "").strip(),
                    city=(row.get("city") or "").strip(),
                    state=(row.get("state") or "").strip(),
                    zip_code=(row.get("zip_code") or "").strip(),
                    industry=(row.get("industry") or "").strip(),
                    line_type=(row.get("line_type") or "").strip(),
                    source=(row.get("source") or "").strip(),
                    created_at=(row.get("created_at") or "").strip(),
                )
            )
    return leads


def filter_leads(
    leads: Iterable[Lead],
    *,
    only_line_types: set[str],
    start_index: int,
    limit: int,
) -> list[Lead]:
    filtered = [lead for lead in leads if not only_line_types or lead.line_type.lower() in only_line_types]
    if start_index > 1:
        filtered = filtered[start_index - 1 :]
    if limit > 0:
        filtered = filtered[:limit]
    return filtered


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "lead"


def build_lead_intro(lead: Lead) -> str:
    company = lead.company_name or "unknown company"
    contact = f" Contact name {lead.contact_name}." if lead.contact_name else ""
    city = f" City {lead.city}." if lead.city else ""
    line_type = f" Line type {lead.line_type}." if lead.line_type else ""
    source = f" Source {lead.source}." if lead.source else ""
    return f"Next Aloha outreach target is {company}.{contact}{city}{line_type}{source} Connecting now."


def build_call_namespace(
    *,
    lead: Lead,
    lead_number: str,
    caller_id: str,
    voice: str,
    timeout: int,
    record: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        intro=build_lead_intro(lead),
        lead_name=lead.contact_name,
        company=lead.company_name,
        voice=voice,
        caller_id=caller_id,
        timeout=timeout,
        record=record,
        lead_number=lead_number,
    )


def start_bridge_call(
    *,
    client,
    agent_number: str,
    caller_id: str,
    voice: str,
    timeout: int,
    lead: Lead,
    status_callback: str,
):
    lead_number = normalize_phone_number(lead.phone_number, "lead number")
    call_args = build_call_namespace(
        lead=lead,
        lead_number=lead_number,
        caller_id=caller_id,
        voice=voice,
        timeout=timeout,
        record=True,
    )
    twiml = build_twiml(call_args)
    create_kwargs = {
        "to": agent_number,
        "from_": caller_id,
        "twiml": twiml,
    }
    if status_callback:
        create_kwargs["status_callback"] = status_callback
        create_kwargs["status_callback_event"] = ["initiated", "ringing", "answered", "completed"]
    call = client.calls.create(**create_kwargs)
    return call, twiml


def wait_for_call_completion(client, call_sid: str, poll_seconds: int, max_wait_seconds: int):
    started_at = time.time()
    while True:
        call = client.calls(call_sid).fetch()
        status = (call.status or "").lower()
        if status in TERMINAL_CALL_STATUSES:
            return call
        if time.time() - started_at >= max_wait_seconds:
            raise TimeoutError(f"Call {call_sid} did not reach a terminal state within {max_wait_seconds} seconds.")
        time.sleep(poll_seconds)


def fetch_child_call(client, parent_call_sid: str):
    child_calls = client.calls.list(parent_call_sid=parent_call_sid, limit=20)
    if not child_calls:
        return None
    child_calls.sort(key=lambda item: getattr(item, "date_created", None) or datetime.min.replace(tzinfo=timezone.utc))
    return child_calls[0]


def fetch_recordings(client, parent_call_sid: str, child_call_sid: str | None) -> list:
    seen: set[str] = set()
    recordings = []
    for call_sid in [parent_call_sid, child_call_sid]:
        if not call_sid:
            continue
        for recording in client.recordings.list(call_sid=call_sid, limit=20):
            if recording.sid in seen:
                continue
            seen.add(recording.sid)
            recordings.append(recording)
    recordings.sort(key=lambda item: getattr(item, "date_created", None) or datetime.min.replace(tzinfo=timezone.utc))
    return recordings


def wait_for_recordings(
    client,
    parent_call_sid: str,
    child_call_sid: str | None,
    poll_seconds: int,
    max_wait_seconds: int,
) -> list:
    started_at = time.time()
    while True:
        recordings = fetch_recordings(client, parent_call_sid, child_call_sid)
        if recordings:
            return recordings
        if time.time() - started_at >= max_wait_seconds:
            return []
        time.sleep(poll_seconds)


def build_recording_download_url(account_sid: str, recording_sid: str) -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Recordings/{recording_sid}.mp3"


def download_recording(account_sid: str, auth_token: str, recording_sid: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    auth = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    request = Request(build_recording_download_url(account_sid, recording_sid))
    request.add_header("Authorization", f"Basic {auth}")
    with urlopen(request) as response, output_path.open("wb") as handle:
        handle.write(response.read())


def download_recording_with_retry(
    account_sid: str,
    auth_token: str,
    recording_sid: str,
    output_path: Path,
    poll_seconds: int,
    max_wait_seconds: int,
) -> None:
    started_at = time.time()
    while True:
        try:
            download_recording(account_sid, auth_token, recording_sid, output_path)
            return
        except HTTPError as exc:
            if exc.code != 404 or time.time() - started_at >= max_wait_seconds:
                raise
            time.sleep(poll_seconds)


def resolve_results_path(args: argparse.Namespace) -> Path:
    if args.results_path:
        return Path(args.results_path)
    DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_RESULTS_DIR / f"batch-results-{stamp}.csv"


def resolve_recordings_dir(args: argparse.Namespace) -> Path:
    if args.recordings_dir:
        path = Path(args.recordings_dir)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = DEFAULT_RECORDINGS_DIR / f"batch-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_results_header(results_path: Path) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=result_fieldnames())
        writer.writeheader()


def append_result(results_path: Path, row: dict[str, str]) -> None:
    with results_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=result_fieldnames())
        writer.writerow(row)


def result_fieldnames() -> list[str]:
    return [
        "attempted_at_utc",
        "lead_row_number",
        "lead_id",
        "company_name",
        "contact_name",
        "lead_number",
        "city",
        "line_type",
        "source",
        "parent_call_sid",
        "parent_status",
        "parent_duration_seconds",
        "child_call_sid",
        "child_status",
        "child_answered_by",
        "child_duration_seconds",
        "recording_sid",
        "recording_duration_seconds",
        "recording_local_path",
        "error",
    ]


def lead_to_console_line(lead: Lead) -> str:
    bits = [f"#{lead.row_number}", lead.company_name or "Unknown company", lead.phone_number]
    if lead.city:
        bits.append(lead.city)
    if lead.line_type:
        bits.append(lead.line_type)
    return " | ".join(bits)


def prompt_continue(next_position: int, total: int) -> bool:
    while True:
        answer = input(f"Continue to lead {next_position} of {total}? [y/n]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def dry_run(leads: list[Lead], results_path: Path, recordings_dir: Path) -> int:
    print(f"Selected {len(leads)} lead(s).")
    for lead in leads:
        print(f"  {lead_to_console_line(lead)}")
    print("")
    print(f"Results CSV: {results_path}")
    print(f"Recordings dir: {recordings_dir}")
    return 0


def main() -> int:
    load_environment()
    args = parse_args()

    if not args.agent_number:
        print(
            "An agent callback number is required. Pass --agent-number or define TWILIO_BRIDGE_AGENT_NUMBER.",
            file=sys.stderr,
        )
        return 1

    csv_path = Path(args.csv_path)
    if not csv_path.exists():
        print(f"Lead CSV not found: {csv_path}", file=sys.stderr)
        return 1

    try:
        agent_number = normalize_phone_number(args.agent_number, "agent number")
        caller_id = normalize_phone_number(args.caller_id, "caller ID")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.start_index < 1:
        print("--start-index must be 1 or greater.", file=sys.stderr)
        return 1

    leads = load_leads(csv_path)
    only_line_types = {item.strip().lower() for item in args.only_line_types.split(",") if item.strip()}
    selected_leads = filter_leads(
        leads,
        only_line_types=only_line_types,
        start_index=args.start_index,
        limit=args.limit,
    )
    if not selected_leads:
        print("No leads matched the current filters.", file=sys.stderr)
        return 1

    results_path = resolve_results_path(args)
    recordings_dir = resolve_recordings_dir(args)

    if args.dry_run:
        return dry_run(selected_leads, results_path, recordings_dir)

    write_results_header(results_path)
    client = create_client()
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")

    print(f"Loaded {len(selected_leads)} lead(s) from {csv_path}")
    print(f"Results will be written to {results_path}")
    print(f"Recordings will be downloaded to {recordings_dir}")

    for index, lead in enumerate(selected_leads, start=1):
        attempted_at = datetime.now(timezone.utc).isoformat()
        print("")
        print(f"Starting lead {index} of {len(selected_leads)}")
        print(f"  {lead_to_console_line(lead)}")

        row = {
            "attempted_at_utc": attempted_at,
            "lead_row_number": str(lead.row_number),
            "lead_id": lead.id,
            "company_name": lead.company_name,
            "contact_name": lead.contact_name,
            "lead_number": lead.phone_number,
            "city": lead.city,
            "line_type": lead.line_type,
            "source": lead.source,
            "parent_call_sid": "",
            "parent_status": "",
            "parent_duration_seconds": "",
            "child_call_sid": "",
            "child_status": "",
            "child_answered_by": "",
            "child_duration_seconds": "",
            "recording_sid": "",
            "recording_duration_seconds": "",
            "recording_local_path": "",
            "error": "",
        }

        try:
            call, _ = start_bridge_call(
                client=client,
                agent_number=agent_number,
                caller_id=caller_id,
                voice=args.voice,
                timeout=args.timeout,
                lead=lead,
                status_callback=args.status_callback,
            )
            row["parent_call_sid"] = call.sid
            print(f"  Parent call SID: {call.sid}")

            completed_call = wait_for_call_completion(
                client,
                call.sid,
                args.poll_seconds,
                args.max_wait_seconds,
            )
            row["parent_status"] = completed_call.status or ""
            row["parent_duration_seconds"] = str(completed_call.duration or "")

            child_call = fetch_child_call(client, call.sid)
            if child_call is not None:
                row["child_call_sid"] = child_call.sid
                row["child_status"] = child_call.status or ""
                row["child_answered_by"] = getattr(child_call, "answered_by", "") or ""
                row["child_duration_seconds"] = str(child_call.duration or "")

            recordings = wait_for_recordings(
                client,
                call.sid,
                row["child_call_sid"] or None,
                args.poll_seconds,
                args.recording_wait_seconds,
            )
            if recordings:
                recording = recordings[-1]
                row["recording_sid"] = recording.sid
                row["recording_duration_seconds"] = str(recording.duration or "")
                filename = f"{lead.row_number:03d}-{slugify(lead.company_name)}-{recording.sid}.mp3"
                local_path = recordings_dir / filename
                download_recording_with_retry(
                    account_sid,
                    auth_token,
                    recording.sid,
                    local_path,
                    args.poll_seconds,
                    args.recording_wait_seconds,
                )
                row["recording_local_path"] = str(local_path)
                print(f"  Recording downloaded: {local_path}")
            else:
                print("  No recording was available for this call.")

        except (TimeoutError, TwilioRestException, ValueError, HTTPError, URLError) as exc:
            row["error"] = str(exc)
            print(f"  Error: {exc}", file=sys.stderr)
        except Exception as exc:  # pragma: no cover - defensive logging for live operations
            row["error"] = f"Unexpected error: {exc}"
            print(f"  Unexpected error: {exc}", file=sys.stderr)

        append_result(results_path, row)
        print(f"  Result saved to {results_path}")

        if index < len(selected_leads) and not args.auto_continue:
            if not prompt_continue(index + 1, len(selected_leads)):
                print("Stopping batch at operator request.")
                break

    print("")
    print("Batch complete.")
    print(f"Results CSV: {results_path}")
    print(f"Recordings dir: {recordings_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
