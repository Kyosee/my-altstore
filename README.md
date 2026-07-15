# Personal AltStore Source

A personal, auto-updating AltStore / SideStore subscription source for my own use.

A daily [GitHub Actions workflow](.github/workflows/update.yml) regenerates
[`apps.json`](apps.json) from the tracked upstream GitHub releases. It never
re-hosts any binary — every `downloadURL` points directly at the upstream
project's official release asset, and each version's `sha256` is computed from
that real file and verified by AltStore at install time.

## Add the source

```
https://raw.githubusercontent.com/Kyosee/my-altstore/master/apps.json
```

## Manual regeneration

```bash
python3 scripts/update_source.py
# optional: export GITHUB_TOKEN=... to raise the API rate limit
```

## License

[MIT](LICENSE).
