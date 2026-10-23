The default build uses public registries and the committed dependency locks.
For an offline local build, the trusted operator may override the `build_inputs`
named context with a retained, hashed Python wheelhouse and npm `_cacache`.
Only lockfile-approved public package artifacts belong there. No credentials,
dotenv files, application databases or arbitrary scripts are accepted inputs.
The mounts are temporary BuildKit inputs; package caches are not final layers.
