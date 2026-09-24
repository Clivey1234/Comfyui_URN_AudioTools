import { app } from "../../scripts/app.js";

const NODE_ID = "URNSaveAudioWithLyrics";

function isOurNode(node) {
    return node?.comfyClass === NODE_ID || node?.constructor?.comfyClass === NODE_ID;
}

function hideLegacySourceFilenameHint(node) {
    const widget = node?.widgets?.find?.((w) => w?.name === "source_filename_hint");
    if (!widget || widget.__urnHidden) return;

    widget.__urnHidden = true;
    widget.value = "";
    widget.computeSize = () => [0, -4];
    widget.draw = () => {};
    widget.serializeValue = () => "";
}

function install(node) {
    if (!isOurNode(node) || node.__urnSaveAudioLyricsInstalled) return;
    node.__urnSaveAudioLyricsInstalled = true;
    hideLegacySourceFilenameHint(node);
}

app.registerExtension({
    name: "URN.AudioNodes.SaveAudioWithLyrics",
    nodeCreated(node) { install(node); },
    loadedGraphNode(node) { install(node); },
    setup() {
        for (const node of app.graph?._nodes || []) install(node);
    },
});
