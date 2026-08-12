"""Local session files.

An agent driving Synchri through a shell cannot realistically thread a secret
through every command, so ``join`` records it in a 0600 file under the
workspace and later commands pick it up automatically.

Precedence, strongest first: explicit flag, environment variable, session file.
Nothing here weakens the broker's checks — it only decides which credential to
present.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..broker import Credential
from ..config import Workspace, write_private
from ..errors import ValidationError

ENV_ROOM = "SYNCHRI_ROOM"
ENV_PARTICIPANT = "SYNCHRI_PARTICIPANT"
ENV_SECRET = "SYNCHRI_SECRET"
ENV_ROOM_TOKEN = "SYNCHRI_ROOM_TOKEN"
LEGACY_ENV_ROOM = "AIDAPTER_ROOM"
LEGACY_ENV_PARTICIPANT = "AIDAPTER_PARTICIPANT"
LEGACY_ENV_SECRET = "AIDAPTER_SECRET"
LEGACY_ENV_ROOM_TOKEN = "AIDAPTER_ROOM_TOKEN"

CURRENT_ROOM_FILE = "current_room"


@dataclass
class SessionRecord:
    room_id: str
    participant: str
    participant_id: str
    secret: str
    kind: str = "agent"
    room_name: str = ""
    room_token: str | None = None

    def to_dict(self) -> dict:
        return {
            "room_id": self.room_id,
            "participant": self.participant,
            "participant_id": self.participant_id,
            "secret": self.secret,
            "kind": self.kind,
            "room_name": self.room_name,
            **({"room_token": self.room_token} if self.room_token else {}),
        }


def save(workspace: Workspace, record: SessionRecord) -> str:
    path = workspace.session_path(record.room_id, record.participant)
    write_private(path, json.dumps(record.to_dict(), indent=2) + "\n")
    return str(path)


def load(workspace: Workspace, room_id: str, participant: str) -> dict | None:
    try:
        path = workspace.session_path(room_id, participant)
    except ValidationError:
        return None
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupted session file
        return None


def list_for_room(workspace: Workspace, room_id: str) -> list[dict]:
    records = []
    if not workspace.sessions_dir.exists():
        return records
    for path in sorted(workspace.sessions_dir.glob(f"{room_id}.*.json")):
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):  # pragma: no cover
            continue
    return records


def set_current_room(workspace: Workspace, room_id: str) -> None:
    write_private(workspace.home / CURRENT_ROOM_FILE, room_id + "\n")


def get_current_room(workspace: Workspace) -> str | None:
    path = workspace.home / CURRENT_ROOM_FILE
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def resolve_room(workspace: Workspace, explicit: str | None, broker=None) -> str:
    """Work out which room a command is about.

    Order: the ``--room`` flag, then ``$SYNCHRI_ROOM``, then the most recent
    **active room bound to the repository you are standing in**, then the last
    room created in this workspace.

    The repo lookup is what makes a new session resume the right conversation
    without anyone remembering a room id.
    """
    room_id = explicit or os.environ.get(ENV_ROOM) or os.environ.get(LEGACY_ENV_ROOM)
    if not room_id and broker is not None:
        here = broker.rooms_for_workspace()
        if here:
            room_id = here[0]["room_id"]
    room_id = room_id or get_current_room(workspace)
    if not room_id:
        raise ValidationError(
            "no room specified: pass --room, set SYNCHRI_ROOM, run inside a repository "
            "that has an active room, or create a room first"
        )
    return room_id.strip()


def resolve_credential(
    workspace: Workspace,
    room_id: str,
    participant: str | None,
    secret: str | None = None,
    room_token: str | None = None,
) -> Credential:
    """Assemble the credential to present to the broker."""
    participant = (
        participant
        or os.environ.get(ENV_PARTICIPANT)
        or os.environ.get(LEGACY_ENV_PARTICIPANT)
        or None
    )
    secret = secret or os.environ.get(ENV_SECRET) or os.environ.get(LEGACY_ENV_SECRET) or None
    room_token = (
        room_token
        or os.environ.get(ENV_ROOM_TOKEN)
        or os.environ.get(LEGACY_ENV_ROOM_TOKEN)
        or None
    )

    if participant and not secret:
        record = load(workspace, room_id, participant)
        if record:
            secret = record.get("secret")
    if not room_token:
        # A room token stored alongside a human session enables observer reads.
        for record in list_for_room(workspace, room_id):
            if record.get("room_token"):
                room_token = record["room_token"]
                break
    return Credential(participant=participant, secret=secret, room_token=room_token)
