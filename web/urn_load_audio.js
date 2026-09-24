import { app } from "../../scripts/app.js";

const NODE_ID = "URNLoadAudio";

function ensureAudioUI(nodeData) {
    if (!nodeData?.input?.required) return;

    const required = nodeData.input.required;
    if (!required.audio) return;

    // ComfyUI's AUDIOUPLOAD widget expects an AUDIO_UI widget to exist before
    // the upload control is constructed. Core injects this for its own LoadAudio
    // node, but custom audio-upload nodes must provide it themselves.
    if (required.audioUI) return;

    const reordered = {};
    let inserted = false;

    for (const [name, spec] of Object.entries(required)) {
        if (name === "audioUI") continue;
        reordered[name] = spec;
        if (name === "audio") {
            reordered.audioUI = ["AUDIO_UI", {}];
            inserted = true;
        }
    }

    if (!inserted) {
        reordered.audioUI = ["AUDIO_UI", {}];
    }

    nodeData.input.required = reordered;
}

app.registerExtension({
    name: "URN.LoadAudio.AudioUploadCompat",

    addCustomNodeDefs(defs) {
        const nodeData = defs?.[NODE_ID];
        if (nodeData) ensureAudioUI(nodeData);
    },

    beforeRegisterNodeDef(_nodeType, nodeData) {
        if (nodeData?.name === NODE_ID) {
            ensureAudioUI(nodeData);
        }
    },
});
