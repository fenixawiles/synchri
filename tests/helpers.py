"""Test helpers shared across modules.

Kept out of ``conftest.py`` so test modules can import it by name without
relative-import gymnastics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from synchri.broker import Broker, Credential
from synchri.models.envelope import MessageDraft


@dataclass
class RoomHarness:
    """A room with a human plus named agents, and their credentials."""

    broker: Broker
    room_id: str
    observer_token: str
    credentials: dict = field(default_factory=dict)

    def credential(self, name: str) -> Credential:
        return self.credentials[name]

    def invite(self, name: str, kind: str = "agent", ttl_seconds: int = 3600) -> dict:
        """Mint an invite as the room's human."""
        return self.broker.create_invite(
            self.room_id,
            name,
            credential=self.credential("human"),
            kind=kind,
            ttl_seconds=ttl_seconds,
        )

    def add_agent(self, name: str, kind: str = "agent") -> Credential:
        invite = self.invite(name, kind=kind)
        result = self.broker.join(invite["token"], name)
        credential = Credential(participant=name, secret=result["secret"])
        self.credentials[name] = credential
        return credential

    def send(self, sender: str, content: str = "hello", **kwargs) -> dict:
        hold = kwargs.pop("hold_floor", False)
        draft = MessageDraft(content=content, **kwargs)
        return self.broker.send(
            self.room_id, credential=self.credential(sender), draft=draft, hold_floor=hold
        )

    def queue_up(self, name: str, priority: str = "queued") -> dict:
        """Put a participant in the waiting queue.

        Only meaningful while somebody else holds the floor — an idle room
        promotes the requester straight to active speaker.
        """
        return self.broker.request_floor(
            self.room_id, credential=self.credential(name), priority=priority
        )

    def pass_turn(self, sender: str, reason: str | None = None) -> dict:
        return self.broker.pass_turn(
            self.room_id, credential=self.credential(sender), reason=reason
        )

    def state(self, name: str) -> str:
        return self.broker.turn_status(self.room_id, credential=self.credential(name))["state"]

    def turn(self, name: str) -> dict:
        return self.broker.turn_status(self.room_id, credential=self.credential(name))

    def status(self) -> dict:
        return self.broker.room_status(self.room_id, credential=self.credential("human"))

    def queue_names(self) -> list[str]:
        return [entry["participant"] for entry in self.status()["queue"]]

    def active_speaker(self) -> str | None:
        return self.status()["active_speaker"]

    def messages(self) -> list[dict]:
        return self.broker.read(self.room_id, credential=self.credential("human"))["messages"]

    def events(self, event_type: str | None = None) -> list[dict]:
        found = self.broker.events(self.room_id, credential=self.credential("human"))["events"]
        return [e for e in found if event_type is None or e["event_type"] == event_type]

    def observer(self) -> Credential:
        return Credential(room_token=self.observer_token)


def make_room(broker: Broker, *agents: str, name: str = "Test Room", **kwargs) -> RoomHarness:
    created = broker.create_room(name, **kwargs)
    harness = RoomHarness(
        broker=broker, room_id=created["room_id"], observer_token=created["observer_token"]
    )
    harness.credentials["human"] = Credential(
        participant=created["human"]["name"], secret=created["human"]["secret"]
    )
    for agent in agents:
        harness.add_agent(agent)
    return harness
