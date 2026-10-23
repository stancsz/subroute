# Relay acceptance fixture contract (not the Northstar skill)

This tiny file exists only to exercise Relay's configured instruction-file mount.
It is not the installed Northstar skill and makes no claim about Northstar behavior.
The executable beneath `/usr/local/bin/claude` is a deterministic test fixture,
not a model or provider client. Follow only the explicit JSON assignment supplied
by this acceptance test; do not access network services or files outside `/workspace`.
