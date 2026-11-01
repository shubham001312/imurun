/* demo.js — drives demo.html. External file (no inline script) so the page
   obeys a strict `default-src 'self'` CSP, like the design-lab UI. */
(function () {
  "use strict";
  var A = window.ImuAperture;

  /* the lockup: the 26 px live mark beside the wordmark, as in the site nav */
  var lockup = document.getElementById("lockup");
  var holder = document.createElement("span");
  lockup.insertBefore(holder, lockup.firstChild);
  A.mount(holder, { size: 26, live: true });

  /* every size the website uses, all live, all on the shared clock */
  var sizes = [
    [18, "loader"], [24, "wordmark"], [26, "nav"], [30, "footer"], [64, "spot"], [128, "hero"]
  ];
  var row = document.getElementById("sizes");
  sizes.forEach(function (s) {
    var fig = document.createElement("figure");
    A.mount(fig, { size: s[0], live: true, title: "Aperture mark, " + s[0] + " px" });
    var cap = document.createElement("figcaption");
    cap.textContent = s[0] + " px · " + s[1];
    fig.appendChild(cap);
    row.appendChild(fig);
  });

  var replay = document.getElementById("replay");
  var cancel = null;
  function play() {
    if (cancel) cancel();
    window.scrollTo(0, 0);
    /* restart the .imu-rise stagger too */
    document.documentElement.classList.remove("has-intro");
    void document.documentElement.offsetWidth;
    cancel = A.intro({ onDone: function () { cancel = null; } });
  }
  replay.addEventListener("click", play);
  play();
})();
