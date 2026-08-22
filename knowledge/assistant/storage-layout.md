# Storage layout

The operating system and Docker image layers are on microSD. Persistent portal
data, media originals, documents, generated videos, databases, model files, and
Assistant conversations belong on the external drive under
`/srv/data/family-photos`.

The storage sentinel is `/srv/data/family-photos/.david-pi-storage`. Portal and
local-model services must fail closed when the verified external mount is
missing. Deployment backups protect source and databases, but media originals
and documents still need a physically independent backup destination.

