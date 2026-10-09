# Multilingual installer (ru/en)

## Language setting

Add to answers.conf before the first installation:

    INSTALL_LANGUAGE=en

or:

    INSTALL_LANGUAGE=ru

When omitted, ru is used for backward compatibility. Unsupported values are rejected. Language is part of the installation configuration fingerprint: changing answers.conf and rerunning the installer is NOT an in-place language migration.

## Scope

The selection controls generated recorder button labels, statuses and application-defined errors, the recordings sign-in page, protected recordings gallery, publisher HTML, and translated installer diagnostics. English dates use YYYY-MM-DD HH:MM:SS. User-generated room names and employee names are not translated. Element Web/Desktop's own interface and operating-system/package-manager messages retain their own language settings. Technical third-party exceptions and protocol error codes are not translated.

The installer contains both translation dictionaries and original embedded source comments; choosing English does not remove all Cyrillic from the downloadable source file. Additional languages require a reviewed dictionary and tests; only ru/en are currently supported.

## Installation

Only for a new Ubuntu host. Do not run over the working production server.

    chmod 700 install.sh
    chmod 600 /root/answers.conf
    set -o pipefail
    bash ./install.sh --check /root/answers.conf 2>&1 | tee /root/install-check.log
    bash ./install.sh --install /root/answers.conf 2>&1 | tee /root/install.log

Replace example domain/IP and provide TLS files first. SERVER_IP must be assigned to the new server. The existing recorder implementations and image pins are retained. This installer downloads missing images and builds its custom recorder components, as the supplied installer does; it is not the offline archive restore procedure.

Use answers.example rather than the older template containing postgres:15, traefik:2.11 and legacy JWT images. Keep generated credential sidecar files private and retain them for a retry.

## Changes

- INSTALL_LANGUAGE=ru/en validated before configuration fingerprint calculation.
- One central English translation dictionary; source localization runs after embedded fixes and before syntax checks/builds.
- Existing button behavior tests localized together with the button.
- Language saved in config/install-language.json.
- Fixed damaged quoting in the supplied embedded recorder source; fallback test hostname replaced with the configured deployment hostname during generation.
- Existing local IP checks, TLS normalization and name-search setting retained.

## Validation performed

- bash -n on final install.sh.
- Python AST parsing of the installer.
- Python AST/Node syntax checks of generated ru/en recorder, controller, pool, gallery and publisher source.
- ru/en gallery render checks for labels and preservation/escaping of Cyrillic user room names.

Not tested here: Docker installation, deployment on Ubuntu, browser rendering screenshots, remote ICE/media or end-to-end recording. After installation, perform a two-PC call and record/stop/playback test.
