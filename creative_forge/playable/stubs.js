// Injected before any script of the playable (Playwright add_init_script). Stubs every network SDK a playable may
// call, and records what it does: CTA/exit calls, navigation attempts, sound, and the user interactions before them.
// __FORGE_CONFIG__ is replaced with {"network": ..., "mraidVersion": ...}.
(function () {
  var cfg = __FORGE_CONFIG__;
  var rec = (window.__forge = {
    interactions: 0,
    firstInteractionAt: null,
    calls: [], // {api, t, interactions, arg}
    audio: [], // {what, t, interactions}
    errors: [],
  });
  var t0 = Date.now();
  function now() { return Date.now() - t0; }
  function call(api, arg) {
    rec.calls.push({ api: api, t: now(), interactions: rec.interactions, arg: arg === undefined ? null : String(arg) });
  }
  // A trusted pointer/touch/key event is a user interaction, counted in the capture phase before the page sees it.
  // One tap fires pointerdown, touchstart and an emulated mousedown: events within 300 ms count once.
  rec._last = -1e9;
  ["pointerdown", "touchstart", "mousedown", "keydown"].forEach(function (type) {
    window.addEventListener(type, function (e) {
      if (!e.isTrusted || now() - rec._last < 300) return;
      rec._last = now();
      rec.interactions += 1;
      if (rec.firstInteractionAt === null) rec.firstInteractionAt = now();
    }, true);
  });

  // ---- MRAID (2.0 / 3.0 subset): ready after load, always viewable
  if (cfg.network !== "tiktok") {
    var listeners = {};
    var state = "loading";
    var mraid = {
      getVersion: function () { return cfg.mraidVersion || "3.0"; },
      getState: function () { return state; },
      isViewable: function () { return true; },
      getPlacementType: function () { return "interstitial"; },
      addEventListener: function (ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      removeEventListener: function (ev, fn) {
        listeners[ev] = (listeners[ev] || []).filter(function (f) { return f !== fn; });
      },
      open: function (url) { call("mraid.open", url); },
      close: function () { call("mraid.close"); },
      expand: function () {},
      resize: function () {},
      useCustomClose: function () {},
      setOrientationProperties: function () {},
      getOrientationProperties: function () { return { allowOrientationChange: true, forceOrientation: "none" }; },
      getMaxSize: function () { return { width: innerWidth, height: innerHeight }; },
      getScreenSize: function () { return { width: screen.width, height: screen.height }; },
      getCurrentPosition: function () { return { x: 0, y: 0, width: innerWidth, height: innerHeight }; },
      getDefaultPosition: function () { return { x: 0, y: 0, width: innerWidth, height: innerHeight }; },
      supports: function () { return false; },
      getAudioVolume: function () { return 100; },
      storePicture: function () {},
      createCalendarEvent: function () {},
      playVideo: function (url) { call("mraid.playVideo", url); },
    };
    function fire(ev, arg) { (listeners[ev] || []).slice().forEach(function (fn) { try { fn(arg); } catch (e) { setTimeout(function () { throw e; }); } }); }
    window.mraid = mraid;
    window.addEventListener("load", function () {
      setTimeout(function () {
        state = "default";
        fire("ready");
        fire("stateChange", "default");
        fire("viewableChange", true);
        fire("exposureChange", 100);
      }, 50);
    });
  }
  // ---- Meta, Google, TikTok, Mintegral
  window.FbPlayableAd = { onCTAClick: function () { call("FbPlayableAd.onCTAClick"); } };
  window.ExitApi = { exit: function () { call("ExitApi.exit"); } };
  window.openAppStore = function () { call("openAppStore"); };
  window.install = function () { call("install"); };
  window.gameEnd = function () { call("gameEnd"); };
  window.gameReady = function () { call("gameReady"); };
  // ---- redirects and messages
  window.open = function (url) { call("window.open", url); return null; };
  var post = window.postMessage.bind(window);
  window.postMessage = function (msg) {
    if (msg === "download" || (msg && msg.type === "download")) call("postMessage:download");
    return post.apply(window, arguments);
  };
  // ---- sound before the first interaction
  function audioMark(what) { rec.audio.push({ what: what, t: now(), interactions: rec.interactions }); }
  try {
    var start = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function () { audioMark("AudioBufferSourceNode.start"); return start.apply(this, arguments); };
    var oscStart = OscillatorNode.prototype.start;
    OscillatorNode.prototype.start = function () { audioMark("OscillatorNode.start"); return oscStart.apply(this, arguments); };
    var play = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      if (!this.muted && this.volume > 0 && (this.tagName === "AUDIO" || this.tagName === "VIDEO")) audioMark(this.tagName.toLowerCase() + ".play");
      return play.apply(this, arguments);
    };
  } catch (e) {}
})();
