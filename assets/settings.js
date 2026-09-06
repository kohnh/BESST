// <details> has no outside-click or Escape close of its own.
const closeSettings = () => document.querySelectorAll("details.set[open]").forEach((d) => (d.open = false));
document.addEventListener("click", (e) => { if (!e.target.closest("details.set")) closeSettings(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSettings(); });

// The live chart runs with plotly's own doubleClick disabled: its default "reset+autosize" toggles between the
// range the figure was FIRST drawn with and the full data extent, neither of which is the selected window preset,
// and it moved the axes a beat before the server's redraw corrected them -- two jumps for one gesture. Handing the
// double-click to the server instead makes it exactly one, onto whichever preset is selected now.
//
// Detected from mousedown, not dblclick: plotly's drag layer calls preventDefault() on mousedown, so the browser
// never synthesises a dblclick over the plot at all. 300 ms is plotly's own DBLCLICKDELAY.
let lastDown = 0;
document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#live-fig")) return;
  const t = e.timeStamp;
  if (t - lastDown < 300) { document.getElementById("rst").click(); lastDown = 0; } else { lastDown = t; }
}, true);
