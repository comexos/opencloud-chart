# Integration tests

`tests/e2e/run.sh` runs a separate suite on a throwaway kind cluster with Keycloak and OpenLDAP. It exercises token-based sign-in, roles, LDAP groups, file transfers, companions, persistent state and externally managed service secrets. It requires kind, kubectl, Helm, Docker and Python and is not run by CI. The harness unit tests (`python3 tests/e2e/test_harness.py`) do not start a cluster.

`tests/e2e/shared/run.sh` tests `topology: split` with two storage pods on one NFS volume, on a kind cluster with three nodes: NFS semantics across nodes (content, extended attributes, `flock`), then parallel uploads and renames through both storage pods, the replacement of each pod, and scaling the storage pods from two to one and back with `helm upgrade` on the same claim. It runs an NFS server in the cluster, which needs the `nfsd` kernel module on the host (`sudo modprobe nfsd`), or uses an external export with `NFS_SERVER=host:/path`. Kind needs a container engine that allows NFS mounts, such as rootful Docker; see the script header for its options.

