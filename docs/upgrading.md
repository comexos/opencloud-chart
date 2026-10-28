# Upgrading

Before an upgrade, back up config and data volumes, external service secrets and any S3 data. Test restoration. Review upstream application release notes as well as chart changes.

## Chart 0.2.1: S3 value names

These names changed; no compatibility aliases are provided before chart 1.0:

| Previous value | New value |
| --- | --- |
| `storage.s3.accessKey` | `storage.s3.accessKeyId` |
| `storage.s3.accessKeySecretKey` | `storage.s3.accessKeyIdSecretKey` |

The default key read from an existing Secret also changed from `accessKey` to `accessKeyId`. Rename that Secret key or explicitly keep its old name:

```yaml
storage:
  s3:
    accessKeyIdSecretKey: accessKey
```

## Changing topology

For non-shared storage, keep all persistence settings unchanged to retain the same claims.

To move from `single` to `split`:

1. Configure external OIDC and LDAP. Preserve existing service secrets with `scripts/service-secrets.py from-config`, then set `serviceSecrets.existingSecret`.
2. Deploy external NATS using the [scaling requirements](scaling.md#nats).
3. Stop new uploads and wait for pending work to finish.
4. Upgrade with `topology: split` and `nats.address`.

To return to `single`, set `topology: single`, `nats.address: ""`, `storagePods.replicas: 1` and `persistence.data.shared: false`. Keep external identity and service secrets. Queued events do not move between the external and embedded NATS servers.

### Entering or leaving shared storage

This is a storage migration, not just a topology change. Kubernetes cannot change StatefulSet volume-claim templates in place.

1. Stop traffic and all writers, and take a consistent backup.
2. Prepare the target claim. Copy data if necessary, preserving ownership and extended attributes. Shared mode requires suitable `ReadWriteMany` storage.
3. Set `persistence.data.existingClaim` to the claim holding the data. Set `shared` and the replica count for the target topology. Preserve the config claim as well.
4. Recreate the StatefulSet with the new volume layout during the maintenance window. Verify claim retention before deleting anything; do not delete the data claims.
5. Verify files, shares, search and uploads before reopening access. Retain the backup until the migration is confirmed.

A shared claim created by the chart has `helm.sh/resource-policy: keep`; it remains after the chart stops declaring it. Manage its ownership explicitly if it will be used by another release.
