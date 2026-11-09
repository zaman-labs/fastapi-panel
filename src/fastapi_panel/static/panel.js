"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function loadCollapsedGroups() {
  try {
    return new Set(JSON.parse(localStorage.getItem("fastapi-panel-nav-collapsed")));
  } catch {
    return new Set();
  }
}

function saveCollapsedGroups(collapsed) {
  try {
    localStorage.setItem("fastapi-panel-nav-collapsed", JSON.stringify([...collapsed]));
  } catch {}
}

function setupChrome() {
  $("[data-toggle-theme]")?.addEventListener("click", () => {
    const root = document.documentElement;
    const isDark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = isDark ? "light" : "dark";
    try {
      localStorage.setItem("fastapi-panel-theme", root.dataset.theme);
    } catch {}
  });

  $("[data-toggle-sidebar]")?.addEventListener("click", () => {
    $("#sidebar").classList.toggle("open");
  });

  const groups = $$("[data-nav-group]");
  const collapsed = loadCollapsedGroups();
  const restoreGroup = (group) => {
    group.open = !collapsed.has(group.dataset.navGroup) || $(".nav-item.active", group) !== null;
  };
  groups.forEach((group) => {
    restoreGroup(group);
    $("summary", group).addEventListener("click", () => {
      // The click fires before the group toggles, so it's still in its old state here.
      if (group.open) collapsed.add(group.dataset.navGroup);
      else collapsed.delete(group.dataset.navGroup);
      saveCollapsedGroups(collapsed);
    });
  });

  $("[data-nav-filter]")?.addEventListener("input", (event) => {
    const term = event.target.value.toLowerCase();
    groups.forEach((group) => {
      const items = $$("[data-nav-item]", group);
      items.forEach((item) => {
        item.hidden = !$("a", item).textContent.toLowerCase().includes(term);
      });
      group.hidden = items.every((item) => item.hidden);
      if (term) group.open = true;
      else restoreGroup(group);
    });
  });
}

function setupChangelist() {
  // On small screens the filters sit above the table, so only keep the ones in use open.
  if (matchMedia("(max-width: 1000px)").matches) {
    $$(".filter:not([data-active])").forEach((filter) => (filter.open = false));
  }

  const form = $("[data-changelist]");
  if (!form) return;

  const rows = $$("[data-row-check]", form);
  const all = $("[data-select-all]", form);
  const counter = $("[data-counter]", form);
  const acrossButton = $("[data-select-across]", form);
  const acrossInput = $("[data-across-input]", form);

  const refresh = () => {
    const count = rows.filter((row) => row.checked).length;
    const allOnPage = rows.length > 0 && count === rows.length;
    if (all) all.checked = allOnPage;
    if (counter) counter.textContent = `${count} of ${rows.length} selected`;
    if (acrossButton) acrossButton.hidden = !allOnPage;
    if (acrossInput && !allOnPage) acrossInput.value = "";
    rows.forEach((row) => row.closest("tr").classList.toggle("selected", row.checked));
  };

  rows.forEach((row) => row.addEventListener("change", refresh));
  all?.addEventListener("change", () => {
    rows.forEach((row) => (row.checked = all.checked));
    refresh();
  });
  acrossButton?.addEventListener("click", () => {
    acrossInput.value = "1";
    counter.textContent = `All ${acrossButton.textContent.replace("Select all ", "")} selected`;
    acrossButton.hidden = true;
  });

  form.addEventListener("submit", (event) => {
    if (event.submitter?.name !== "_run_action") return;
    const option = $("[data-action-select]", form).selectedOptions[0];
    if (option?.dataset.confirm && !confirm(option.dataset.confirm)) event.preventDefault();
  });
}

function setupPrepopulate(root = document) {
  const slugify = (text) =>
    text
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");

  $$("[data-prepopulate]", root).forEach((target) => {
    const sources = target.dataset.prepopulate.split(",").map((name) => document.getElementsByName(name)[0]);
    let touched = target.value !== "";
    target.addEventListener("input", () => (touched = target.value !== ""));
    sources.forEach((source) =>
      source?.addEventListener("input", () => {
        if (!touched) target.value = slugify(sources.map((s) => s?.value ?? "").join(" "));
      }),
    );
  });
}

function setupAutocomplete(root = document) {
  $$("[data-autocomplete]", root).forEach((box) => {
    const search = $("[data-search]", box);
    const results = $("[data-results]", box);
    const chips = $("[data-chips]", box);
    const multiple = "multiple" in box.dataset;
    let timer;

    const addChip = (id, text) => {
      if (!multiple) chips.replaceChildren();
      if ($$("input", chips).some((input) => input.value === id)) return;
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.dataset.chip = "";
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = box.dataset.name;
      input.value = id;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.remove = "";
      remove.setAttribute("aria-label", `Remove ${text}`);
      remove.textContent = "×";
      chip.append(input, text, remove);
      chips.append(chip);
    };

    box.addEventListener("panel:choose", (event) => addChip(event.detail.id, event.detail.label));

    chips.addEventListener("click", (event) => {
      if (event.target.matches("[data-remove]")) event.target.closest("[data-chip]").remove();
    });

    const load = async () => {
      const url = `${box.dataset.autocomplete}?q=${encodeURIComponent(search.value)}`;
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      results.replaceChildren(
        ...data.results.map((item) => {
          const li = document.createElement("li");
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = item.text;
          button.addEventListener("click", () => {
            addChip(item.id, item.text);
            search.value = "";
            results.hidden = true;
          });
          li.append(button);
          return li;
        }),
      );
      results.hidden = data.results.length === 0;
    };

    search.addEventListener("focus", load);
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(load, 200);
    });
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
      if (event.key === "Escape") results.hidden = true;
    });
    document.addEventListener("click", (event) => {
      if (!box.contains(event.target)) results.hidden = true;
    });
  });
}

function setupDualSelects(root = document) {
  $$("select[data-dual]", root).forEach((select) => {
    select.hidden = true;
    const wrapper = document.createElement("div");
    wrapper.className = "dual";

    const makeColumn = (title) => {
      const column = document.createElement("div");
      column.className = "dual-column";
      const heading = document.createElement("strong");
      heading.textContent = title;
      const list = document.createElement("select");
      list.multiple = true;
      list.size = 8;
      list.setAttribute("aria-label", title);
      column.append(heading, list);
      return { column, list };
    };

    const left = makeColumn("Available");
    const right = makeColumn("Chosen");
    const filter = document.createElement("input");
    filter.type = "search";
    filter.placeholder = "Filter";
    filter.setAttribute("aria-label", "Filter available options");
    left.column.insertBefore(filter, left.list);

    const render = () => {
      const term = filter.value.toLowerCase();
      left.list.replaceChildren();
      right.list.replaceChildren();
      [...select.options].forEach((option) => {
        const copy = new Option(option.text, option.value);
        if (option.selected) right.list.add(copy);
        else if (option.text.toLowerCase().includes(term)) left.list.add(copy);
      });
    };

    const move = (list, selected) => {
      const values = new Set([...list.selectedOptions].map((option) => option.value));
      [...select.options].forEach((option) => {
        if (values.has(option.value)) option.selected = selected;
      });
      render();
    };

    const buttons = document.createElement("div");
    buttons.className = "dual-buttons";
    [
      ["→", "Add selected", () => move(left.list, true)],
      ["←", "Remove selected", () => move(right.list, false)],
    ].forEach(([text, label, handler]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button";
      button.textContent = text;
      button.setAttribute("aria-label", label);
      button.addEventListener("click", handler);
      buttons.append(button);
    });

    filter.addEventListener("input", render);
    select.addEventListener("change", render);
    filter.addEventListener("keydown", (event) => {
      if (event.key === "Enter") event.preventDefault();
    });
    left.list.addEventListener("dblclick", () => move(left.list, true));
    right.list.addEventListener("dblclick", () => move(right.list, false));

    wrapper.append(left.column, buttons, right.column);
    select.after(wrapper);
    render();
  });
}

function setupFileFields(root = document) {
  $$("[data-file-field]", root).forEach((box) => {
    const input = $("input[type=file]", box);
    const preview = $("[data-file-preview]", box);
    const error = $("[data-file-error]", box);
    input.addEventListener("change", () => {
      const file = input.files[0];
      const tooLarge = file && file.size > Number(input.dataset.maxSize);
      error.hidden = !tooLarge;
      error.textContent = tooLarge ? "This file is too large." : "";
      if (tooLarge) input.value = "";
      if (preview.src) URL.revokeObjectURL(preview.src);
      const showPreview = file && !tooLarge && file.type.startsWith("image/") && file.type !== "image/svg+xml";
      preview.hidden = !showPreview;
      if (showPreview) preview.src = URL.createObjectURL(file);
    });
  });
}

function setupRichText(root = document) {
  const commands = [
    ["B", "Bold", () => document.execCommand("bold")],
    ["I", "Italic", () => document.execCommand("italic")],
    ["U", "Underline", () => document.execCommand("underline")],
    ["H2", "Heading", () => document.execCommand("formatBlock", false, "<h2>")],
    ["H3", "Subheading", () => document.execCommand("formatBlock", false, "<h3>")],
    ["¶", "Paragraph", () => document.execCommand("formatBlock", false, "<p>")],
    ["• List", "Bulleted list", () => document.execCommand("insertUnorderedList")],
    ["1. List", "Numbered list", () => document.execCommand("insertOrderedList")],
    ["❝", "Quote", () => document.execCommand("formatBlock", false, "<blockquote>")],
  ];

  $$("textarea[data-richtext]", root).forEach((source) => {
    source.hidden = true;
    const box = document.createElement("div");
    box.className = "rich";
    const toolbar = document.createElement("div");
    toolbar.className = "rich-toolbar";
    toolbar.setAttribute("role", "toolbar");
    const editor = document.createElement("div");
    editor.className = "rich-content rich-editor";
    editor.contentEditable = "true";
    editor.setAttribute("role", "textbox");
    editor.setAttribute("aria-multiline", "true");
    // The server sanitizes this value before it gets here, and the CSP blocks scripts anyway.
    editor.innerHTML = source.value;
    const label = source.id && $(`label[for="${CSS.escape(source.id)}"]`);
    if (label) {
      editor.setAttribute("aria-label", label.textContent.trim());
      label.addEventListener("click", () => editor.focus());
    }

    const sync = () => {
      source.value = editor.innerHTML;
    };
    const run = (command) => {
      editor.focus();
      // shortcut: execCommand is deprecated but has no replacement short of shipping an editor library
      document.execCommand("defaultParagraphSeparator", false, "p");
      command();
      sync();
    };
    const addButton = (text, title, handler) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "rich-button";
      button.textContent = text;
      button.title = title;
      button.setAttribute("aria-label", title);
      // Keeps the text selected while the button is pressed.
      button.addEventListener("mousedown", (event) => event.preventDefault());
      button.addEventListener("click", handler);
      toolbar.append(button);
    };
    commands.forEach(([text, title, command]) => addButton(text, title, () => run(command)));

    const linkBar = document.createElement("div");
    linkBar.className = "rich-link";
    linkBar.hidden = true;
    const linkInput = document.createElement("input");
    linkInput.type = "url";
    linkInput.placeholder = "https://example.com";
    linkInput.setAttribute("aria-label", "Link address");
    const linkApply = document.createElement("button");
    linkApply.type = "button";
    linkApply.className = "button button-small";
    linkApply.textContent = "Add link";
    linkBar.append(linkInput, linkApply);

    let selection = null;
    const applyLink = () => {
      const address = linkInput.value.trim();
      linkBar.hidden = true;
      linkInput.value = "";
      if (!selection) return;
      editor.focus();
      const current = getSelection();
      current.removeAllRanges();
      current.addRange(selection);
      if (address) {
        const url = /^(https?:|mailto:)/i.test(address) ? address : `https://${address}`;
        run(() => document.execCommand("createLink", false, url));
      }
    };
    addButton("Link", "Link", () => {
      const current = getSelection();
      selection = current.rangeCount && editor.contains(current.anchorNode) ? current.getRangeAt(0) : null;
      linkBar.hidden = !selection || !linkBar.hidden;
      if (!linkBar.hidden) linkInput.focus();
    });
    addButton("Unlink", "Remove link", () => run(() => document.execCommand("unlink")));
    linkApply.addEventListener("click", applyLink);
    linkInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        applyLink();
      }
      if (event.key === "Escape") linkBar.hidden = true;
    });

    editor.addEventListener("input", sync);
    editor.addEventListener("paste", (event) => {
      // Pasted documents bring their own markup, so only the text comes along.
      event.preventDefault();
      document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
    });
    box.append(toolbar, linkBar, editor);
    source.after(box);
  });
}

function chooseCreated(button, id, label) {
  const box = button.closest("[data-related]");
  const search = $("[data-autocomplete]", box);
  if (search) {
    search.dispatchEvent(new CustomEvent("panel:choose", { detail: { id, label } }));
    return;
  }
  const url = button.dataset.relatedAdd;
  // Every dropdown for this model gets the new option, such as the other rows of an inline.
  $$("[data-related-add]")
    .filter((other) => other.dataset.relatedAdd === url)
    .forEach((other) => {
      const select = $("select", other.closest("[data-related]"));
      if (select && ![...select.options].some((option) => option.value === id)) select.add(new Option(label, id));
    });
  const select = $("select", box);
  if (select) {
    [...select.options].find((option) => option.value === id).selected = true;
    select.dispatchEvent(new Event("change"));
  } else {
    $("input", box).value = id;
  }
}

function setupRelatedAdd() {
  const result = $("[data-popup-result]");
  if (result) {
    const { id, label } = result.dataset;
    window.parent.postMessage({ type: "panel-created", id, label }, location.origin);
    return;
  }
  $("[data-popup-cancel]")?.addEventListener("click", () => {
    window.parent.postMessage({ type: "panel-cancel" }, location.origin);
  });

  let open = null;

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-related-add]");
    if (!button) return;
    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    const head = document.createElement("div");
    head.className = "modal-head";
    const title = document.createElement("strong");
    title.textContent = button.dataset.relatedTitle;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "link-button";
    close.textContent = "×";
    close.setAttribute("aria-label", "Close");
    close.addEventListener("click", () => dialog.close());
    head.append(title, close);
    const frame = document.createElement("iframe");
    frame.title = button.dataset.relatedTitle;
    frame.src = button.dataset.relatedAdd;
    dialog.append(head, frame);
    dialog.addEventListener("close", () => {
      dialog.remove();
      open = null;
    });
    document.body.append(dialog);
    dialog.showModal();
    open = { dialog, frame, button };
  });

  window.addEventListener("message", (event) => {
    if (!open || event.origin !== location.origin || event.source !== open.frame.contentWindow) return;
    if (event.data?.type === "panel-created") chooseCreated(open.button, event.data.id, event.data.label);
    if (event.data?.type === "panel-created" || event.data?.type === "panel-cancel") open.dialog.close();
  });
}

function setupInlines() {
  $$("[data-inline]").forEach((inline) => {
    const rows = $("[data-inline-rows]", inline);
    const total = $("[data-inline-total]", inline);
    const blank = $("[data-inline-blank]", inline);
    let next = Number(inline.dataset.nextIndex);

    $("[data-inline-add]", inline)?.addEventListener("click", () => {
      const holder = document.createElement("div");
      holder.innerHTML = blank.innerHTML.replaceAll("__prefix__", String(next));
      const row = $("[data-inline-template]", holder);
      row.removeAttribute("data-inline-template");
      row.setAttribute("data-inline-row", "");
      rows.append(row);
      next += 1;
      total.value = String(next);
      setupPrepopulate(row);
      setupAutocomplete(row);
      setupDualSelects(row);
      setupFileFields(row);
      setupRichText(row);
    });

    // Removed rows leave a gap in the numbering. The server skips blanks, so that's fine.
    inline.addEventListener("click", (event) => {
      const remove = event.target.closest("[data-inline-remove]");
      if (remove) remove.closest("[data-inline-row]").remove();
    });
  });
}

function formState(form) {
  // Files count by name: enough to see that one was picked.
  return new URLSearchParams(
    [...new FormData(form)].map(([name, value]) => [name, typeof value === "string" ? value : value.name]),
  ).toString();
}

function setupLiveUpdates() {
  const notice = $("[data-live-updates]");
  if (!notice) return;
  const seen = Number(notice.dataset.total);
  const url = notice.dataset.countUrl + location.search;
  // shortcut: compares totals, so a delete and an add in the same interval cancel out; track the newest row if that matters.
  const timer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(url, { credentials: "same-origin", redirect: "error" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const added = (await response.json()).total - seen;
      if (added <= 0) return;
      const word = added === 1 ? notice.dataset.name : notice.dataset.namePlural;
      $("[data-live-text]", notice).textContent = `${added} new ${word}.`;
      notice.hidden = false;
    } catch {
      // Logged out, lost access or the server is gone: stop asking rather than retry forever.
      clearInterval(timer);
    }
  }, Number(notice.dataset.liveUpdates) * 1000);
}

function setupUnsavedWarning() {
  const form = $("[data-change-form]");
  if (!form) return;
  // Compared with a snapshot, not set on any input, so typing in a search box or undoing an edit doesn't count.
  const saved = formState(form);
  let submitting = false;
  form.addEventListener("submit", () => {
    submitting = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (submitting || formState(form) === saved) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

setupChrome();
setupChangelist();
setupLiveUpdates();
setupPrepopulate();
setupAutocomplete();
setupDualSelects();
setupFileFields();
setupRichText();
setupInlines();
setupRelatedAdd();
// Last, so the snapshot sees fields the other setups filled in.
setupUnsavedWarning();

document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !confirm(message)) event.preventDefault();
});
