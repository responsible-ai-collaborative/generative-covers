/* Gallery of generated covers. Reads manifest.json (written by the GitHub Actions job) and
   lets editors preview each image and copy its Cloudinary URL or public ID. No dependencies. */
(() => {
  "use strict";

  const THUMB = "c_fill,w_720,h_480,f_auto,q_auto";
  const LARGE = "c_limit,w_1600,f_auto,q_auto";

  const $ = (sel, root = document) => root.querySelector(sel);
  const grid = $("#grid");
  const notice = $("#notice");
  const stats = $("#stats");
  const count = $("#count");
  const footerMeta = $("#footer-meta");
  const search = $("#search");
  const onlyNeeded = $("#only-needed");
  const sort = $("#sort");
  const dialog = $("#preview");
  const toast = $("#toast");
  const template = $("#card-template");

  let items = [];
  let manifest = null;
  let toastTimer = null;

  /* ---------- helpers ---------- */
  const transformed = (url, t) => url.replace("/image/upload/", `/image/upload/${t}/`);
  const fmtBytes = (n) => (n == null ? "" : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`);
  const fmtDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const showToast = (text) => {
    toast.textContent = text;
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, 1800);
  };
  const copyText = async (text, button) => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", "");
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      showToast("Copied to clipboard");
      if (button) {
        const label = button.textContent;
        button.dataset.done = "1";
        button.textContent = "Copied";
        setTimeout(() => { delete button.dataset.done; button.textContent = label; }, 1400);
      }
    } catch (err) {
      showToast("Copy failed. Select the text and copy it manually.");
    }
  };
  const badgeFor = (item) => {
    if (item.incident_has_report_image === false) return ["needed", "still needs an image"];
    if (item.incident_has_report_image === true) return ["has-image", "incident now has an image"];
    return ["", ""];
  };

  /* ---------- rendering ---------- */
  function render() {
    const q = search.value.trim().toLowerCase();
    const needed = onlyNeeded.checked;
    let list = items.filter((item) => {
      if (needed && item.incident_has_report_image !== false) return false;
      if (!q) return true;
      return String(item.incident_id) === q.replace(/^#/, "") ||
        String(item.incident_id).includes(q) ||
        (item.title || "").toLowerCase().includes(q);
    });
    const [field, dir] = sort.value.split("-");
    list.sort((a, b) => {
      let diff;
      if (field === "generated") diff = new Date(a.created_at || 0) - new Date(b.created_at || 0);
      else diff = a.incident_id - b.incident_id;
      return dir === "desc" ? -diff : diff;
    });

    grid.replaceChildren(...list.map(cardFor));
    count.textContent = list.length === items.length
      ? `${items.length} cover${items.length === 1 ? "" : "s"}`
      : `${list.length} of ${items.length} covers`;
    notice.hidden = list.length > 0 || items.length === 0;
    if (items.length > 0 && list.length === 0) notice.textContent = "No covers match this search.";

    const params = new URLSearchParams();
    if (q) params.set("q", search.value.trim());
    if (needed) params.set("needed", "1");
    if (sort.value !== "incident-desc") params.set("sort", sort.value);
    const qs = params.toString();
    history.replaceState(null, "", (qs ? `?${qs}` : location.pathname) + location.hash);
  }

  function cardFor(item) {
    const node = template.content.firstElementChild.cloneNode(true);
    const img = $("img", node);
    img.src = transformed(item.url, THUMB);
    img.alt = `Generated cover for incident ${item.incident_id}`;
    img.width = 720; img.height = 480;
    $(".card-image", node).addEventListener("click", () => openPreview(item));

    const link = $(".incident-link", node);
    link.href = item.incident_url;
    link.textContent = `Incident ${item.incident_id}`;
    const [state, label] = badgeFor(item);
    const badge = $(".badge", node);
    badge.textContent = label;
    if (state) badge.dataset.state = state;

    $(".card-title", node).textContent = item.title || "(untitled incident)";
    const meta = [];
    if (item.created_at) meta.push(`generated ${fmtDate(item.created_at)}`);
    if (item.width && item.height) meta.push(`${item.width}×${item.height}`);
    $(".card-meta", node).textContent = meta.join(" · ");

    $('[data-action="copy-url"]', node).addEventListener("click", (e) => copyText(item.url, e.currentTarget));
    $('[data-action="copy-id"]', node).addEventListener("click", (e) => copyText(item.public_id, e.currentTarget));
    return node;
  }

  function openPreview(item) {
    const img = $("#preview-img");
    img.src = transformed(item.url, LARGE);
    img.alt = `Generated cover for incident ${item.incident_id}`;
    $("#preview-incident").textContent = `Incident ${item.incident_id}${item.incident_date ? ` · ${item.incident_date}` : ""}`;
    $("#preview-title").textContent = item.title || "(untitled incident)";
    $("#preview-url").value = item.url;
    $("#preview-id").value = item.public_id;
    $("#preview-open").href = item.url;
    $("#preview-cite").href = item.incident_url;

    const rows = [
      ["Status", badgeFor(item)[1] || "unknown (snapshot unavailable)"],
      ["Generated", item.created_at ? fmtDate(item.created_at) : ""],
      ["Model", item.model ? `${item.model}${item.quality ? ` (${item.quality})` : ""}` : ""],
      ["Dimensions", item.width && item.height ? `${item.width} × ${item.height} px` : ""],
      ["File", [item.format ? item.format.toUpperCase() : "", fmtBytes(item.bytes)].filter(Boolean).join(", ")],
      ["Prompt version", item.prompt_version || ""],
    ].filter(([, v]) => v);
    $("#preview-meta").replaceChildren(...rows.flatMap(([k, v]) => {
      const dt = document.createElement("dt"); dt.textContent = k;
      const dd = document.createElement("dd"); dd.textContent = v;
      return [dt, dd];
    }));
    history.replaceState(null, "", `${location.pathname}${location.search}#incident-${item.incident_id}`);
    if (typeof dialog.showModal === "function") dialog.showModal();
    else window.open(item.url, "_blank", "noopener");
  }

  /* ---------- load ---------- */
  async function load() {
    const params = new URLSearchParams(location.search);
    if (params.get("q")) search.value = params.get("q");
    if (params.get("needed") === "1") onlyNeeded.checked = true;
    if (params.get("sort")) sort.value = params.get("sort");

    try {
      const resp = await fetch("manifest.json", { cache: "no-cache" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      manifest = await resp.json();
    } catch (err) {
      notice.hidden = false;
      notice.innerHTML = "No <code>manifest.json</code> was found next to this page. The GitHub Actions job writes it " +
        "when it publishes the site; locally, run <code>python -m generative_covers manifest</code> first.";
      count.textContent = "";
      return;
    }
    items = Array.isArray(manifest.items) ? manifest.items : [];
    if (manifest.error) {
      notice.hidden = false;
      notice.textContent = `The listing could not be built: ${manifest.error}`;
      count.textContent = "";
      stats.textContent = "";
      footerMeta.textContent = manifest.generated_at ? `Attempted ${new Date(manifest.generated_at).toLocaleString()}` : "";
      return;
    }

    const needed = manifest.incidents_without_report_images;
    const covered = items.filter((i) => i.incident_has_report_image === false).length;
    const bits = [`<strong>${items.length}</strong> generated cover${items.length === 1 ? "" : "s"}`];
    if (needed != null) {
      bits.push(`<strong>${needed}</strong> incident${needed === 1 ? "" : "s"} currently without a report image`);
      bits.push(`<strong>${covered}</strong> of those have a cover here`);
    }
    stats.innerHTML = bits.join(" · ");

    const metaBits = [];
    if (manifest.generated_at) metaBits.push(`Listing updated ${new Date(manifest.generated_at).toLocaleString()}`);
    if (manifest.snapshot_key) metaBits.push(`AIID snapshot ${manifest.snapshot_key.replace(/^backup-|\.tar\.bz2$/g, "")}`);
    if (manifest.cloud_name) metaBits.push(`Cloudinary ${manifest.cloud_name}/${manifest.folder}`);
    footerMeta.textContent = metaBits.join(" · ");

    if (items.length === 0) {
      notice.hidden = false;
      notice.textContent = "No covers have been generated yet. Run the “Generate incident covers” workflow in GitHub Actions.";
    }
    render();

    const deepLink = location.hash.match(/^#incident-(\d+)$/);
    if (deepLink) {
      const item = items.find((i) => i.incident_id === Number(deepLink[1]));
      if (item) openPreview(item);
    }
  }

  search.addEventListener("input", render);
  onlyNeeded.addEventListener("change", render);
  sort.addEventListener("change", render);
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    $("#preview-img").removeAttribute("src");
    if (location.hash.startsWith("#incident-")) history.replaceState(null, "", location.pathname + location.search);
  });
  dialog.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", (e) => copyText($(button.dataset.copy).value, e.currentTarget));
  });

  load();
})();
