import {config} from "./config_init.js";

/**
 * settings.js:
 *
 * A data store used to store active settings of the GDS and providing those settings as data for the rest of the
 * system. These settings are typically advanced but should be tracked for the user.
 */
class Settings {
    constructor() {
        this.miscellaneous = {
            event_buffer_size: -1,
            command_buffer_size: -1,
            response_object_limit: 6000,
            compact_commanding: false,
            channels_display_last_received: true
         };
         this.polling_intervals = {};
         // Transport for the events/channels/command_history endpoints.
         // "stream" uses a WebSocket push from the GDS (when available),
         // "poll" uses the legacy REST polling. Controlled by the server's
         // --ui-initial-transport flag and applied on each page load via
         // applyServerDefaultTransport().
         this.transport = {
             mode: "stream",
         };
    }

    /**
     * Apply the server-provided default transport on page load. Called
     * after probing /api/stream/status so the CLI flag
     * (--ui-initial-transport) controls the browser's transport each
     * time.
     */
    applyServerDefaultTransport(mode) {
         if (mode === "stream" || mode === "poll") {
             this.transport.mode = mode;
         }
    }

    /**
     * Set the transport mode. Called by the Advanced Settings dropdown.
     */
    setTransport(mode) {
         if (mode !== "stream" && mode !== "poll") {
             return;
         }
         this.transport.mode = mode;
    }

    /**
     * Setup the polling settings from a set of keys representing pollers. Keys should be supplied as a list of names
     * and not as a list of anonymous objects.
     * @param keys: list of keys to setup polling settings.
     */
    setupPollingSettings(keys) {
        let _self = this;
        keys.forEach((key) => {
            _self.polling_intervals[key] = config.dataPollIntervalsMs[key] || config.dataPollIntervalsMs.default || 1000;
        });
    }
}
export let _settings = new Settings();
