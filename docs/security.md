# Security model (v0.1)

## Threat model

**The adversary AIDapter v0.1 defends against is a confused or misbehaving local agent.**

Everything runs as you, on your machine, in your home directory. An agent participating
in a room is a process you started, with your permissions. It can already read your
files. The realistic failure modes are therefore:

- an agent acting in a room it was never invited to
- an agent speaking out of turn and corrupting the exchange
- an agent that you removed continuing to act
- one room's contents leaking into another room
- a hostile room or participant name escaping the workspace directory

Those are what the controls below address.

**Not in the threat model for v0.1:** another OS user on the same machine, a local
attacker who can read `~/.aidapter`, a remote attacker (there is no network surface),
or a malicious agent that simply refuses to cooperate with the protocol.

## Controls

### Identifiers and secrets

- Room ids, participant ids, message ids: `secrets.token_urlsafe(12)` — 96 bits, prefixed
  by type. Nothing sequential, nothing derived from a timestamp or a counter.
- Room join tokens and participant secrets: `secrets.token_urlsafe(32)` — 256 bits.
- Only `sha256(salt + "." + secret)` is stored, with a distinct 128-bit salt per record.
  Plaintext is returned once, at creation or join, and never persisted by the broker.
  A test asserts the plaintext never appears in the database file.

**On not using a slow KDF:** bcrypt/scrypt/Argon2 exist to make guessing *low-entropy*
secrets expensive. These secrets are 256 bits of CSPRNG output — there is nothing to
guess. A slow KDF would add latency to every CLI call, including every `wait` poll, and
buy nothing. This is a deliberate decision, not an oversight.

### Room scoping

- Every function in `aidapter/storage/dao.py` takes an explicit `room_id` and filters on
  it. There is no query in the data layer that can return another room's rows.
- A participant secret authenticates `(room_id, participant_id)` together. A credential
  minted in room A is rejected in room B even when the participant name is identical.
- A compound join token embeds its room id; presenting it to a different room is
  rejected outright rather than being tried against that room's hash.
- Join failures return the same message for "wrong token" and "token for another room",
  so the error does not confirm which rooms exist.

### Revocation

`remove` sets a participant's status to `removed`. The secret remains cryptographically
valid — that is the point. Every authenticated operation checks status *separately*
from the credential, so a removed participant is refused even while presenting the
correct secret. Removal also cancels the participant's active turn, drops their queue
entry, and makes their name unclaimable by a subsequent join.

`stop-room` is terminal and authoritative: the room accepts no further mutations from
anyone, including the human who stopped it, and this survives a restart because it is
persisted state rather than a process flag.

### Filesystem

- The workspace (`~/.aidapter`, or `$AIDAPTER_HOME`) is created `0700`. The database,
  session files, ledgers, and transcripts are `0600`.
- Room ids are validated against `^room_[A-Za-z0-9_-]{16,64}$` **before** being used as a
  path component. Room names and participant names never become path components at all,
  so a room named `../../../etc/evil` is stored under its random room id like any other.
- Ledger writes go to a temp file in the same directory and are then `os.replace`d, so a
  concurrent reader sees the old file or the new one, never a partial write.

### Network

**v0.1 opens no socket.** There is no listener to bind, no port to firewall, and no
`--host` flag to get wrong. A test replaces `socket.socket.bind` with an assertion and
drives a full exchange through the broker to prove it.

If a future version adds an observer API, it must bind `127.0.0.1` by default and
require an explicit flag to do otherwise.

### Credentials for providers

None are stored, because v0.1 needs none. AIDapter never talks to a model provider; the
agents do that themselves, with their own configuration. Keeping it that way is a
feature, and any future adapter should preserve it.

## Known weaknesses

Stated so they are decisions rather than surprises:

1. **Session files hold plaintext secrets** at `0600` under the workspace. Anything
   running as you can read them — but anything running as you can read the database too.
   This is a usability trade-off inside the stated threat model.
2. **The room owner's session file also stores the room join token**, so the human can
   run observer reads without flags. Agent session files deliberately do not.
3. **Cooperative protocol.** The broker refuses out-of-turn *writes*, but it cannot make
   an agent poll, honor a handoff, or tell the truth about what it did.
4. **No rate limiting.** A looping agent can fill a transcript. The autonomy budget
   bounds turns, not message size or disk usage.
5. **No encryption at rest.** Room contents sit in a plain SQLite file and plain text
   files.
6. **No audit of reads.** Writes are audited in `events`; reads are not.

## Reporting

This is an early prototype and has not been audited. Please open an issue for anything
that looks wrong; do not use AIDapter for anything sensitive yet.
