
const viewer = document.querySelector(".viewer-svg-wrap");
const svg = viewer ? viewer.querySelector("svg") : null;
const controls = Array.from(document.querySelectorAll("[data-view-mode]"));
let currentMode = "100";

function setActive(mode) {
  controls.forEach((button) => {
    button.classList.toggle("active", button.dataset.viewMode === mode);
  });
}

function viewBoxParts(svg) {
  const parts = (svg.getAttribute("viewBox") || "").trim().split(/\s+/).map(Number);
  if (parts.length === 4 && parts[2] > 0 && parts[3] > 0) {
    return parts;
  }
  return [0, 0, 1200, 900];
}

function viewBoxRatio(svg) {
  const parts = viewBoxParts(svg);
  return parts[3] / parts[2];
}

function viewBoxWidth(svg) {
  return viewBoxParts(svg)[2];
}

function applyMode(mode) {
  if (!viewer || !svg) {
    return;
  }
  svg.style.maxWidth = "none";
  svg.style.display = "block";
  currentMode = mode;
  if (mode === "fit") {
    viewer.style.overflow = "hidden";
    svg.setAttribute("width", "100%");
    svg.setAttribute("height", "100%");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  } else {
    viewer.style.overflow = "auto";
    const scale = mode === "width" ? 1 : Number(mode) / 100;
    const baseWidth = mode === "width" ? viewer.clientWidth : viewBoxWidth(svg);
    const width = Math.max(320, Math.round(baseWidth * scale));
    const height = Math.max(240, Math.round(width * viewBoxRatio(svg)));
    svg.setAttribute("width", `${width}px`);
    svg.setAttribute("height", `${height}px`);
    svg.setAttribute("preserveAspectRatio", "xMidYMin meet");
  }
  setActive(mode);
}

applyMode("100");

window.addEventListener("resize", () => applyMode(currentMode));

controls.forEach((button) => {
  button.addEventListener("click", () => applyMode(button.dataset.viewMode));
});
