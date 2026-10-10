# Contributing

## Fixtures

Use only synthetic fixtures. A fixture has no real mail, no real name, and no personal data. Addresses use `example.com`.

## Privacy

Do not commit bodies, subjects, or addresses from real mail. Do not commit secrets, age keys, or OAuth tokens.

## Done

Open a pull request. On a clean Linux runner with no network credentials, this command must exit 0:

```sh
make check && scripts/proof.sh
```

`scripts/proof.sh` lives under `scripts/`.
