"""Bits of front-end shared by every page in the site.

All three pages (the landing page, the Marketplace board, the auction board) are
independently rendered static HTML with their own inlined CSS and JS — that's what keeps
each one a single file that renders with no network. The cost of that choice is
triplication, so anything genuinely common lives here once and is inlined into all three.
"""

from __future__ import annotations

#: Pull-to-refresh. These pages are static artifacts republished by CI, so "refresh"
#: honestly means "fetch the newly published page" — a reload, not a client-side
#: re-render. On a phone that is exactly the gesture you reach for after a bid moves.
#:
#: Deliberately only arms at the very top of the page and only for touch, so it can
#: never fight an ordinary scroll or a desktop trackpad. The indicator is driven from
#: the gesture itself rather than an animation, so a half-pull that gets abandoned
#: springs back instead of implying a refresh that didn't happen.
PULL_TO_REFRESH_CSS = """
#ptr{position:fixed;top:0;left:0;right:0;display:flex;align-items:center;
  justify-content:center;gap:8px;height:0;overflow:hidden;z-index:60;
  background:var(--card);color:var(--soft);font-size:13px;font-weight:600;
  border-bottom:1px solid var(--line);transition:height .18s ease}
#ptr.armed{color:var(--accent)}
#ptr .spin{width:14px;height:14px;border:2px solid var(--line);
  border-top-color:var(--accent);border-radius:50%}
#ptr.busy .spin{animation:ptrspin .7s linear infinite}
@keyframes ptrspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){#ptr{transition:none}#ptr.busy .spin{animation:none}}
"""

PULL_TO_REFRESH_HTML = '<div id="ptr"><span class="spin"></span><span class="txt"></span></div>'

PULL_TO_REFRESH_JS = r"""
// Pull-to-refresh. The page is a published artifact, so refreshing means re-fetching it.
(function(){
  var el = document.getElementById('ptr');
  if (!el) return;
  var txt = el.querySelector('.txt');
  var startY = 0, pulling = false, dist = 0;
  var THRESHOLD = 70, MAX = 110;

  function atTop(){
    return (window.scrollY || document.documentElement.scrollTop || 0) <= 0;
  }
  function set(h, armed){
    el.style.height = h + 'px';
    el.classList.toggle('armed', !!armed);
    if (txt) txt.textContent = h < 8 ? '' : (armed ? 'Release to refresh' : 'Pull to refresh');
  }

  document.addEventListener('touchstart', function(e){
    // Only arm at the very top, and never mid-gesture with several fingers down.
    if (!atTop() || e.touches.length !== 1) { pulling = false; return; }
    startY = e.touches[0].clientY;
    pulling = true;
    dist = 0;
  }, {passive: true});

  document.addEventListener('touchmove', function(e){
    if (!pulling) return;
    var dy = e.touches[0].clientY - startY;
    if (dy <= 0) { set(0, false); dist = 0; return; }
    if (!atTop()) { pulling = false; set(0, false); return; }
    // Resistance: the pull slows as it lengthens, so it feels anchored rather than loose.
    dist = Math.min(MAX, dy * 0.5);
    set(dist, dist >= THRESHOLD);
  }, {passive: true});

  document.addEventListener('touchend', function(){
    if (!pulling) return;
    pulling = false;
    if (dist >= THRESHOLD) {
      el.classList.add('busy');
      if (txt) txt.textContent = 'Refreshing…';
      set(46, true);
      location.reload();
    } else {
      set(0, false);
    }
  });
})();
"""


def sort_select(options: list[tuple[str, str]], *, select_id: str = "sort") -> str:
    """A labelled sort dropdown. ``options`` is (value, label), first one the default."""
    opts = "".join(f'<option value="{v}">{label}</option>' for v, label in options)
    return (
        f'<label class="sortwrap">Sort '
        f'<select id="{select_id}">{opts}</select></label>'
    )


SORT_CSS = """
.sortwrap{font-size:12.5px;color:var(--soft);display:flex;align-items:center;gap:6px}
.sortwrap select{font:inherit;font-size:13.5px;min-height:40px;padding:8px 12px;
  border-radius:999px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);cursor:pointer}
"""
