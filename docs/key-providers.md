# Key providers

How FlowBuilder encrypts stored secrets, how to rotate the master key, and the design of the
Vault and AWS KMS providers that P06_S09 will build.

## Envelope encryption

Every row of `secrets` is encrypted under its own data key (DEK):

- The value is encrypted with AES-256-GCM under a fresh 32-byte DEK and a random 96-bit nonce.
- The associated data is `workspace_id.bytes + secret_id.bytes`. A row copied to another
  workspace, or another row's ciphertext pasted into it, fails to decrypt instead of leaking a value.
- The DEK is wrapped by a `KeyProvider` (`backend/src/app/crypto/key_provider.py`), bound to the
  same workspace and secret, so a wrapped DEK lifted from one row does not unwrap for another. The
  row stores the wrapped DEK and the `key_id` of the master key (KEK) that wrapped it, never the
  DEK itself.

A new value always gets a new DEK, so no DEK ever encrypts twice and nonce reuse cannot happen.

A `KeyProvider` has three members. `context` is the secret's 32-byte associated data,
`workspace_id.bytes + secret_id.bytes`.

| Member | Contract |
| --- | --- |
| `active_key_id` | The key id that `wrap` uses now. Rotation moves every row to it. |
| `async wrap(dek, context)` | Returns the wrapped DEK and the `key_id` that unwraps it, which must equal `active_key_id`. Rotation refuses to continue if it does not, rather than re-wrapping the same rows forever. |
| `async unwrap(wrapped, key_id, context)` | Returns the DEK, or raises `UnknownKeyIdError` (no such key) or `KeyUnwrapError` (wrong key, wrong context or tampered blob). Messages name the key id only. |

## Local keyring

`LocalKeyProvider` reads its keys from `MASTER_KEYS`, or from the file named by `MASTER_KEYS_FILE`.
Set exactly one of them. The compose stack passes `MASTER_KEYS`; the file form is for deployments
that mount the keyring as a secret file.

- Each key is 32 random bytes in base64: `openssl rand -base64 32`.
- Keys are separated by whitespace. The **first** key wraps new DEKs; the others only unwrap.
- A key's id is `local:` plus the first 16 hex characters of
  `HMAC-SHA256(key, "flowbuilder/key-id")`. Ids never need naming by hand. For a random key the id
  reveals nothing usable; a guessable key would still be guessable from its id, which is why weak
  keys are refused.
- The api and the rotation CLI refuse to start if the keyring is missing, empty, holds a duplicate,
  or holds a key that is not valid base64, not exactly 32 bytes, has fewer than 16 distinct byte
  values, or consists only of printable ASCII. The last two rules reject placeholders such as the
  all-zero key in `.env.example`, and passphrases encoded as base64.

**Back up the keyring outside the database.** Without the key that wrapped a DEK, its secret is lost.

## Rotating the master key

Rotation re-wraps DEKs. It never reads or rewrites a ciphertext, and it leaves `updated_at` alone,
because no secret's value changes.

1. Generate a new key and add it **second** in the keyring, after the current key, so it can only
   unwrap. Deploy every api instance with it.
2. Move the new key **first**, keeping the old key after it, and deploy every api instance again.
   From now on new secrets are written under the new key, and every instance can read both.
   Skipping step 1 breaks a rolling deploy: instances still on the old keyring cannot read what
   the updated ones write.
3. Run the rotation:

   ```
   docker compose --env-file .env -f infra/docker-compose.yml run --build --rm rotate-keys
   ```

   `--build` matters: the service sits in the `maintenance` profile, so `up --build` never rebuilds
   it, and without the flag `run` reuses whatever image an earlier run left, however old its code.

   It prints `rewrapped N data keys to <key_id>; M remain under other keys`, and exits 1 while
   `M` is above 0.
4. Rerun it until it reports `0 remain`. Each batch commits on its own, so an interrupted run loses
   nothing and the next run carries on; a run with nothing to do changes nothing.
5. If it stops with `cannot re-wrap the data key of secret …`, that secret was wrapped under a key
   the keyring no longer holds, and every rerun stops at it. Put the missing key back after the
   active key and rerun. If that key is gone for good, the secret is lost: delete it or store a new
   value for it, then rerun.
6. Only when it reports `0 remain`, remove the old key from the keyring and deploy again.

The CLI connects as the schema owner. Two policies from migration 0006 let the owner select and
update any `secrets` row in every workspace, and no policy lets it insert or delete one. They are a
guard against mistakes, not a security boundary: row-level security limits rows, not columns, and
the owner can alter its own table's policies. Only the CLI's own queries restrict themselves to
`id`, `workspace_id`, `wrapped_dek` and `key_id`. The rotation container holds the owner's
credentials and every master key, so run it only for a rotation.

## Designed, not built: VaultKeyProvider

Uses HashiCorp Vault's Transit secrets engine with an `aes256-gcm96` key.

- `wrap` calls `transit/encrypt/<key>` with `key_version` set to the active version, and
  `associated_data` set to `context`. Pinning the version keeps `wrap`'s `key_id` equal to
  `active_key_id` even after someone rotates the Transit key. `unwrap` calls
  `transit/decrypt/<key>` with the same `associated_data`.
- `key_id` is `vault:<mount>/<key>:v<version>`. `active_key_id` comes from configuration or from
  the key's `latest_version` at startup. Vault decrypts with the version in the ciphertext's
  `vault:vN:` prefix, so `unwrap` checks that the prefix matches `key_id` and raises
  `KeyUnwrapError` if it does not.
- Create the key with `exportable=false`, `allow_plaintext_backup=false` and
  `deletion_allowed=false`.
- After `transit/keys/<key>/rotate`, rotation can call `transit/rewrap/<key>` instead of unwrap and
  wrap, so the DEK never reaches FlowBuilder. Add an optional `rewrap` method to the interface only
  when this provider is built.
- Two identities, authenticated with AppRole or Kubernetes auth. The api's token may only
  `encrypt` and `decrypt` on that one key; only the rotation job's token may `rewrap`.

## Designed, not built: AwsKmsKeyProvider

Uses AWS KMS.

- `wrap` calls `Encrypt` and `unwrap` calls `Decrypt`, with an EncryptionContext of
  `workspace_id`, `secret_id` and `key_id`. A wrapped DEK presented for another secret or under
  another key id fails, and CloudTrail records which workspace each call served.
- `key_id` is `awskms:<key ARN>`. KMS's automatic yearly rotation keeps the ARN and needs no
  re-wrap. Moving to a different key uses `ReEncrypt`, the KMS counterpart of `rewrap`.
- The api's IAM role gets only `kms:Encrypt` and `kms:Decrypt` on that key ARN; only the rotation
  job's role gets `kms:ReEncrypt*`. Key policies can add conditions on
  `kms:EncryptionContext:workspace_id`.

## Rules for both remote providers

- Every call goes through the P01_S06 egress client and its allowlist, with a timeout. A failure
  raises; there is no fallback to another provider or a cached key.
- A remote call inside a database transaction holds a pooled connection, and during rotation row
  locks, for a network round trip. Where the order allows, wrap before opening the transaction, as
  `put` and value replacement can.
- The P06_S09 DEK cache must be keyed on `(workspace_id, secret_id, wrapped_dek)`, never on the
  wrapped DEK alone, so no lookup can return another secret's key.
- Migrating from the local keyring is ordinary rotation: the remote key becomes active, and the
  provider must still unwrap the old `local:` ids until every row has moved.
