# Several storage pods (experimental)

`persistence.data.shared` switches the storage pods to one shared `ReadWriteMany` data volume: a single claim, `<fullname>-data` (or `persistence.data.existingClaim`), mounted by every storage pod instead of one claim per pod, and kept on uninstall. `storagePods.replicas` above 1 requires it:

```yaml
topology: split
storagePods:
  replicas: 2
persistence:
  data:
    shared: true
    accessMode: ReadWriteMany
    storageClass: nfs-rwx
```

- **File system:** OpenCloud keeps its metadata in extended attributes and coordinates writers with `flock`. Use NFS 4.2 mounted with `noac`, or CephFS. NFS 4.1 and older have no extended attributes.
- **Restarts:** while a storage pod restarts, requests routed to it fail for up to about 30 seconds after the new pod is ready (see the [scaling overview](../README.md#scaling)); the data stays intact.
- **search and ocm** hold state that only one process may own (the search index, the OCM invitation store), so they run on the first pod, `<fullname>-0`, only. While that pod restarts, search and OCM are unavailable; file access continues through the other pods.
- **Not supported upstream:** OpenCloud's maintainers name shared `ReadWriteMany` storage as the prerequisite for several instances, but there is no published, tested configuration. `tests/e2e/shared/run.sh` checks this setup on NFS; run it against your storage before relying on it.
- **Scaling:** with shared storage, changing `storagePods.replicas` keeps the same claim, including when scaling down to one pod. Set `shared` before installation if you want this option.

Entering or leaving shared mode changes the volume layout. Stop writers, preserve the data and recreate the StatefulSet. Follow [the upgrade procedure](upgrading.md#entering-or-leaving-shared-storage).

## NATS

In `split`, the pods share OpenCloud's service registry, events, caches and stores through an external NATS cluster. The chart does not deploy NATS. Run one with JetStream, for example with the [official NATS chart](https://github.com/nats-io/k8s/tree/main/helm/charts/nats), and note these OpenCloud 8.1 requirements:

- **Memory storage:** the service registry keeps its data in JetStream memory storage. Without it, the pods fail with `insufficient resources`.
- **No TLS for the registry:** the registry client has no TLS option, so the client port must accept plain connections. Restrict access with a NetworkPolicy.
- **Health checks without credentials:** OpenCloud's readiness checks connect to NATS without the username and password. With authentication enabled, add a `no_auth_user` that may connect but neither publish nor subscribe, or the pods never become ready.
- **Single copies:** OpenCloud creates its buckets and event stream with one replica. A NATS cluster spreads the load, but losing the node that holds them interrupts OpenCloud until that node returns.

Values for the official NATS chart that meet these requirements:

```yaml
config:
  cluster:
    enabled: true
    replicas: 3
  jetstream:
    enabled: true
    fileStore:
      pvc:
        size: 10Gi
    memoryStore:
      enabled: true
      maxSize: 256Mi
  merge:
    authorization:
      users:
        - user: opencloud
          password: "<password from the opencloud-nats Secret>"
        - user: healthcheck
          password: "<random, unused>"
          permissions:
            publish: {deny: [">"]}
            subscribe: {deny: [">"]}
    no_auth_user: healthcheck
```

Then point the chart at it, with the `username` and `password` keys in `nats.existingSecret`:

```yaml
topology: split
nats:
  address: nats.nats.svc.cluster.local:4222
  existingSecret: opencloud-nats
```

