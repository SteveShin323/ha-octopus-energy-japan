# Brand images

The six images [`home-assistant/brands`](https://github.com/home-assistant/brands)
requires for a custom integration, at the sizes it accepts.

| File | Size | Used for |
|---|---|---|
| `icon.png` | 256×256 | the integration's icon |
| `icon@2x.png` | 512×512 | the same, on a high-density display |
| `logo.png` | 752×240 | the integration page, on a light theme |
| `logo@2x.png` | 1503×480 | the same, on a high-density display |
| `dark_logo.png` | 752×240 | the integration page, on a dark theme |
| `dark_logo@2x.png` | 1503×480 | the same, on a high-density display |

`tests/test_manifest.py` pins every size and requires an alpha channel, because a
submission with the wrong dimensions is rejected and the pull request is the only place
that would otherwise surface it.

**These do not belong inside `custom_components/`.** Home Assistant serves brand images
from `brands.home-assistant.io`, never from the component directory, so shipping them
there would add weight to every install for something the integration never reads.

To submit, copy this directory to
`custom_integrations/octopus_energy_japan/` in a fork of `home-assistant/brands` and open
a pull request. That needs this repository to be public first, which is the one
outstanding rule in `quality_scale.yaml`.
