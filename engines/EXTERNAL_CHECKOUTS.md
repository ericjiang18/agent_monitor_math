# External engine checkouts

The following engine source trees are maintained as clean upstream checkouts
and are intentionally not vendored into this repository. Their local virtual
environments, Node runtimes, package caches, and generated logs are also not
source artifacts.

## Danus / Math Agent Harness reference

- Repository: <https://github.com/frenzymath/Danus.git>
- Revision: `0f3f7415518fd515faee79d333ec1dd72834886f`

```bash
git clone https://github.com/frenzymath/Danus.git engines/danus
git -C engines/danus checkout --detach 0f3f7415518fd515faee79d333ec1dd72834886f
```

## DeepSeek Harness

- Repository: <https://github.com/deepseek-ai/deepseek-harness.git>
- Revision: `47f943859bef60e4160492346772ded9b24f765a`

```bash
git clone https://github.com/deepseek-ai/deepseek-harness.git engines/deepseek-harness
git -C engines/deepseek-harness checkout --detach 47f943859bef60e4160492346772ded9b24f765a
```

The ProvingConsole integrations, adapters, routing, tests, and deployment
templates that are maintained by this project remain tracked in this
repository.
