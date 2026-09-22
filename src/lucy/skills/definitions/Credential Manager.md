---
name: Credential Manager
description: 
triggers: password token credential store retrieve secret
category: security
---

# Credential Manager

Securely stores passwords and API tokens encrypted at rest in memory.db (same SQLite DB as conversation memory).

## Encryption

- Algorithm: Fernet (AES-128-CBC + HMAC-SHA256)
- Master key: stored separately in `data/cred.key` (NOT in memory.db)
- Secrets are never logged, listed, or exposed in metadata operations

## Tools

| Tool | Action |
|------|--------|
| `store_credential` | Store an encrypted password or API token |
| `retrieve_credential` | Decrypt and return a stored secret |
| `list_credentials` | List all stored services (name + username only) |
| `delete_credential` | Remove a credential |

## Usage

```bash
# Store a credential
store_credential(service="gmail_app_password", secret_value="my-secret", username="benny")

# List what's stored
list_credentials()

# Retrieve the actual secret
retrieve_credential(service="gmail_app_password")

# Delete
delete_credential(service="gmail_app_password")
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/credentials` | List all services (metadata only) |
| POST | `/api/credentials/{service}` | Retrieve decrypted value |
| POST | `/api/credentials` | Store new (body: service, secret_value, username?) |
| DELETE | `/api/credentials/{service}` | Delete a credential |
| GET | `/api/credentials` | List all stored credential services |