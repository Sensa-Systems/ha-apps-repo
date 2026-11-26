"""Voice control through the Home Assistant Voice device (Vision and AI, no AI model yet).

The Voice device is an ESPHome satellite that Home Assistant's own `esphome`
integration owns. It listens for its wake word itself, then streams the words
that follow to the box, where Home Assistant's Assist pipeline turns them into
text (Whisper), matches them against fixed sentences (Home Assistant's built-in
conversation agent, not a language model) and speaks the answer (Piper). Nothing
leaves the van. The AI tier will later swap in a local model as the agent.

What this module owns, all on a Container appliance with a Voice device
registered (the licence gates the device to Vision and AI when it is added,
`service_class:voice_satellite`, so being registered already means entitled):

* **The speech engines.** Two `wyoming` config entries pointing at the stack's
  Whisper and Piper containers, and a "Sensa" Assist pipeline using them, made
  the preferred one so the Voice device picks it up.
* **What can be said.** Exposure is set here rather than left to Home
  Assistant's defaults, which would make every settings switch speakable. The
  set is DERIVED, never listed by hand: the entities a scene can control (the
  Scenes editor's eligibility pass) plus the battery and tank levels the Home
  dials show, named as the customer sees them. It follows every dashboard
  rebuild, so a rename or a new device reaches voice without anything else.
* **Scenes.** "Sleep mode" runs `sensa_nomad.activate_mode`, through a sentence
  file generated from the scene list and a Python intent handler. A bare
  `scene.turn_on` would skip the licence check and the current-scene latch.
* **Read-only.** Assist's own light and switch calls never pass through Sensa's
  licence check, so a locked box exposes nothing and the scene intent answers
  with a refusal. Keyed on `licence_ok`, which fails open, so an unprimed cold
  boot never silences an entitled van.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ASSISTANT = "conversation"
VOICE_SERVICE_CLASS = "voice_satellite"

# Domains Home Assistant's built-in sentences can drive. The eligibility pass
# also admits select / number / text helpers a scene can set, which have no
# natural spoken form.
VOICE_CONTROL_DOMAINS = frozenset({
    "light", "switch", "climate", "fan", "cover", "valve", "lock",
    "media_player", "water_heater",
})

# The stack's speech containers (tools/images/container/docker-compose.yml).
# Home Assistant runs on the host network, so localhost reaches them.
WHISPER_HOST, WHISPER_PORT = "127.0.0.1", 10300
PIPER_HOST, PIPER_PORT = "127.0.0.1", 10200
PIPELINE_NAME = "Sensa"

INTENT_ACTIVATE_MODE = "SensaActivateMode"
SENTENCES_LANGUAGE = "en"
SENTENCES_FILE = "sensa_nomad.yaml"

LOCKED_SPEECH = "Control is disabled. Your Sensa licence needs attention."


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def voice_device_registered(devices: list[dict[str, Any]]) -> bool:
    """True when any registered device is a Voice satellite."""
    from .vendors.service_classes import classify

    for device in devices or []:
        if not isinstance(device, dict):
            continue
        if (device.get("service_class") or classify(device)) == VOICE_SERVICE_CLASS:
            return True
    return False


def container_appliance(hass: HomeAssistant) -> bool:
    """A Container appliance: the only flavour with the speech containers."""
    from .backup_config import is_container_appliance

    return is_container_appliance(hass)


# --------------------------------------------------------------------------
# What can be said
# --------------------------------------------------------------------------


def voice_targets(
    eligible: list[str],
    names: dict[str, str],
    readouts: list[dict[str, Any]],
) -> dict[str, str | None]:
    """entity_id -> the spoken name to add as an alias (None = its own name).

    `eligible` / `names` are the Scenes editor's controllable entities and their
    display names; `readouts` are the Home dials (`{"entity", "name"}`), which is
    where "Fresh Water" and "Battery" are named for the customer.
    """
    targets: dict[str, str | None] = {}
    for entity_id in eligible or []:
        if entity_id.split(".", 1)[0] not in VOICE_CONTROL_DOMAINS:
            continue
        targets[entity_id] = (names or {}).get(entity_id) or None
    for dial in readouts or []:
        entity_id = dial.get("entity")
        if isinstance(entity_id, str) and entity_id:
            targets[entity_id] = dial.get("name") or None
    return targets


def _home_readouts() -> list[dict[str, Any]]:
    from .dashboard import _home_left_dials

    try:
        return list(_home_left_dials().get("gauges") or [])
    except Exception:  # noqa: BLE001 - a readout is a nicety, never a blocker
        _LOGGER.debug("Voice: Home dials unavailable", exc_info=True)
        return []


def plan_exposure(
    entity_ids: list[str],
    is_exposed: Any,
    targets: dict[str, str | None],
    locked: bool,
) -> list[tuple[str, bool]]:
    """The (entity_id, expose) writes that make exposure exactly `targets`.

    Nothing when `locked`. Every other entity is un-exposed: on an appliance
    Sensa owns the box, and Home Assistant's defaults would make the settings
    switches speakable. Only a change is listed, because each write saves the
    entity registry. `is_exposed(entity_id)` is Home Assistant's current answer.
    """
    wanted = set() if locked else set(targets)
    writes: list[tuple[str, bool]] = []
    for entity_id in entity_ids:
        want = entity_id in wanted
        try:
            if is_exposed(entity_id) != want:
                writes.append((entity_id, want))
        except Exception:  # noqa: BLE001 - one odd entity must not stop the rest
            _LOGGER.debug("Voice: exposure read failed for %s", entity_id, exc_info=True)
    return writes


def alias_to_add(entry: Any, spoken: str | None) -> str | None:
    """The alias to add so `spoken` reaches this entity, or None if it already does."""
    if entry is None or not spoken or not spoken.strip():
        return None
    spoken = spoken.strip()
    known = {a.lower() for a in (entry.aliases or ()) if isinstance(a, str)}
    own = (entry.name or entry.original_name or "").strip().lower()
    if spoken.lower() in known or spoken.lower() == own:
        return None
    return spoken


def async_sync_exposure(
    hass: HomeAssistant,
    targets: dict[str, str | None],
    locked: bool,
) -> int:
    """Apply `plan_exposure` and the spoken-name aliases. Returns the writes."""
    from homeassistant.components.homeassistant.exposed_entities import (
        async_expose_entity,
        async_should_expose,
    )
    from homeassistant.helpers import entity_registry as er

    writes = plan_exposure(
        hass.states.async_entity_ids(),
        lambda entity_id: async_should_expose(hass, ASSISTANT, entity_id),
        targets, locked,
    )
    for entity_id, want in writes:
        try:
            async_expose_entity(hass, ASSISTANT, entity_id, want)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Voice: exposure write failed for %s", entity_id, exc_info=True)
    count = len(writes)
    if locked:
        return count
    ent_reg = er.async_get(hass)
    for entity_id, spoken in targets.items():
        entry = ent_reg.async_get(entity_id)
        alias = alias_to_add(entry, spoken)
        if alias is None:
            continue
        ent_reg.async_update_entity(
            entity_id, aliases=with_alias(entry.aliases, alias, er.COMPUTED_NAME),
        )
        count += 1
    return count


def with_alias(aliases: Any, alias: str, computed_name: Any) -> list[Any]:
    """`aliases` plus `alias`, keeping the entity's own name answerable.

    Home Assistant 2026.8 keeps aliases as an ordered list in which a
    `COMPUTED_NAME` marker stands for the entity's own name, and an EMPTY list
    means "own name only". Writing just the new alias would therefore stop the
    entity answering to its own name, so an empty list starts from the marker.
    """
    current = list(aliases or ()) or [computed_name]
    return [*current, alias]


# --------------------------------------------------------------------------
# Scenes by voice
# --------------------------------------------------------------------------


def mode_sentences(modes: list[dict[str, Any]]) -> dict[str, Any]:
    """The custom sentence file for activating a scene by its spoken name.

    Every scene the customer has, including hidden ones (hiding only takes a
    scene off the tiles), under the name they gave it.
    """
    values = []
    for mode in modes or []:
        slug = str(mode.get("slug") or "").strip()
        name = str(mode.get("name") or "").strip()
        if slug and name:
            values.append({"in": name.lower(), "out": slug})
    return {
        "language": SENTENCES_LANGUAGE,
        "intents": {
            INTENT_ACTIVATE_MODE: {
                "data": [{
                    "sentences": [
                        "[activate|start|switch to|turn on|set] [the] {sensa_mode} (mode|scene)",
                        "[activate|start|switch to] [the] {sensa_mode}",
                        "nomad[,] {sensa_mode} [mode|scene]",
                    ],
                }],
            },
        },
        "lists": {"sensa_mode": {"values": values}},
    }


def _write_sentences(path: Path, text: str) -> bool:
    """Write the file only when it changed. True when it was written."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


async def async_write_mode_sentences(hass: HomeAssistant, modes: list[dict[str, Any]]) -> None:
    """Regenerate the scene sentences and reload the agent when they changed."""
    import yaml

    text = "# Generated by Sensa from the scene list. Edits are overwritten.\n" + yaml.safe_dump(
        mode_sentences(modes), sort_keys=False, allow_unicode=True,
    )
    path = Path(hass.config.path("custom_sentences", SENTENCES_LANGUAGE, SENTENCES_FILE))
    changed = await hass.async_add_executor_job(_write_sentences, path, text)
    if changed and hass.services.has_service("conversation", "reload"):
        await hass.services.async_call("conversation", "reload", {}, blocking=False)


def async_register_intents(hass: HomeAssistant, entry_id: str) -> None:
    """Register the scene intent. Idempotent across reloads."""
    from homeassistant.helpers import intent

    from .const import DOMAIN

    class _ActivateMode(intent.IntentHandler):
        intent_type = INTENT_ACTIVATE_MODE
        description = "Activates a Sensa scene"

        async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
            from . import licence_ok

            response = intent_obj.create_response()
            if not licence_ok(intent_obj.hass, entry_id):
                response.async_set_speech(LOCKED_SPEECH)
                return response
            slug = str(intent_obj.slots.get("sensa_mode", {}).get("value") or "")
            await intent_obj.hass.services.async_call(
                DOMAIN, "activate_mode", {"mode": slug},
                blocking=True, context=intent_obj.context,
            )
            spoken = str(intent_obj.slots.get("sensa_mode", {}).get("text") or slug)
            response.async_set_speech(f"{spoken.capitalize()} scene on")
            return response

    try:
        intent.async_register(hass, _ActivateMode())
    except Exception:  # noqa: BLE001 - already registered by an earlier load
        _LOGGER.debug("Voice: scene intent already registered", exc_info=True)


# --------------------------------------------------------------------------
# Speech engines
# --------------------------------------------------------------------------


async def _async_ensure_wyoming(hass: HomeAssistant, host: str, port: int) -> bool:
    """Create the `wyoming` entry for one container. True once it exists.

    A container still downloading its model refuses the connection, and the
    flow answers `cannot_connect`; that is aborted and retried next tick.
    """
    for entry in hass.config_entries.async_entries("wyoming"):
        if entry.data.get("host") == host and int(entry.data.get("port") or 0) == port:
            return True
    flow = hass.config_entries.flow
    result = await flow.async_init("wyoming", context={"source": "user"})
    flow_id = result.get("flow_id")
    try:
        result = await flow.async_configure(flow_id, {"host": host, "port": port})
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Voice: wyoming %s:%d flow raised", host, port, exc_info=True)
        result = {}
    kind = str(getattr(result.get("type"), "value", result.get("type")) or "")
    if kind == "create_entry":
        _LOGGER.info("Voice: added speech engine at %s:%d", host, port)
        return True
    if kind == "form":
        flow.async_abort(result["flow_id"])
    return False


def _wyoming_engine(hass: HomeAssistant, domain: str, port: int) -> str | None:
    """The stt / tts entity the wyoming entry for `port` created, or None."""
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    for entry in hass.config_entries.async_entries("wyoming"):
        if int(entry.data.get("port") or 0) != port:
            continue
        for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            if ent.domain == domain:
                return ent.entity_id
    return None


async def _async_ensure_pipeline(hass: HomeAssistant, stt_engine: str, tts_engine: str) -> None:
    """A "Sensa" pipeline on the local engines, and the preferred one."""
    from homeassistant.components.assist_pipeline import (
        async_create_default_pipeline,
        async_get_pipelines,
        async_update_pipeline,
    )

    pipeline = next((p for p in async_get_pipelines(hass) if p.name == PIPELINE_NAME), None)
    if pipeline is None:
        pipeline = await async_create_default_pipeline(
            hass, stt_engine_id=stt_engine, tts_engine_id=tts_engine, pipeline_name=PIPELINE_NAME,
        )
        if pipeline is None:
            # An engine does not speak the box's language; nothing to prefer.
            _LOGGER.warning("Voice: the local speech engines do not support this box's language")
            return
        _LOGGER.info("Voice: created the %s Assist pipeline", PIPELINE_NAME)
    elif pipeline.stt_engine != stt_engine or pipeline.tts_engine != tts_engine:
        await async_update_pipeline(hass, pipeline, stt_engine=stt_engine, tts_engine=tts_engine)

    # Preferred, so a Voice device left on "preferred" (its default) uses it.
    # There is no public setter; the store's own method is the one Home
    # Assistant's websocket command calls.
    try:
        from homeassistant.components.assist_pipeline.const import DOMAIN as AP_DOMAIN
        from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE

        store = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
        if store.async_get_preferred_item() != pipeline.id:
            store.async_set_preferred_item(pipeline.id)
            _LOGGER.info("Voice: %s is now the preferred %s pipeline", PIPELINE_NAME, AP_DOMAIN)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Voice: could not make %s the preferred pipeline", PIPELINE_NAME, exc_info=True)


async def async_ensure_speech(hass: HomeAssistant, devices: list[dict[str, Any]]) -> None:
    """Wire the local speech engines into Assist once a Voice device is here."""
    if not container_appliance(hass) or not voice_device_registered(devices):
        return
    whisper = await _async_ensure_wyoming(hass, WHISPER_HOST, WHISPER_PORT)
    piper = await _async_ensure_wyoming(hass, PIPER_HOST, PIPER_PORT)
    if not (whisper and piper):
        return
    stt_engine = _wyoming_engine(hass, "stt", WHISPER_PORT)
    tts_engine = _wyoming_engine(hass, "tts", PIPER_PORT)
    if stt_engine and tts_engine:
        await _async_ensure_pipeline(hass, stt_engine, tts_engine)


# --------------------------------------------------------------------------
# One entry point for the rebuild and the licence flip
# --------------------------------------------------------------------------


async def async_refresh(
    hass: HomeAssistant,
    entry_id: str,
    devices: list[dict[str, Any]],
    eligible: list[str],
    names: dict[str, str],
    modes: list[dict[str, Any]] | None,
) -> None:
    """Bring voice in line with the devices, the scenes and the licence."""
    from . import licence_ok

    if not container_appliance(hass) or not voice_device_registered(devices):
        return
    locked = not licence_ok(hass, entry_id)
    targets = voice_targets(eligible, names, _home_readouts())
    writes = async_sync_exposure(hass, targets, locked)
    if writes:
        _LOGGER.info(
            "Voice: %d exposure change(s); %d entit%s speakable%s",
            writes, 0 if locked else len(targets), "y" if len(targets) == 1 else "ies",
            " (locked: licence needs attention)" if locked else "",
        )
    if modes is not None:
        await async_write_mode_sentences(hass, modes)
