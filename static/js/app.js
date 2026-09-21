/* Atulya Yatra 2.0 — shared front-end interactions. */
(function () {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const navToggle = $(".nav-toggle");
  const navLinks = $(".nav-links");
  if (navToggle && navLinks) {
    navToggle.addEventListener("click", () => {
      const open = navLinks.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", String(open));
    });
  }

  $$(".flash").forEach((element) => {
    setTimeout(() => {
      element.style.transition = "opacity .4s, transform .4s";
      element.style.opacity = "0";
      element.style.transform = "translateX(30px)";
      setTimeout(() => element.remove(), 400);
    }, 4200);
  });

  function initSearch(box) {
    const input = $("input", box);
    const dropdown = $(".autocomplete", box);
    if (!input || !dropdown) return;
    const goButton = $(".search-go", box);
    let items = [];
    let active = -1;
    let timer = null;

    const close = () => { dropdown.classList.remove("open"); active = -1; };

    // The hero clips its children (it has to, so the zooming background photos
    // stay inside it), and the sticky section-nav sits just below. Rather than
    // letting the suggestions spill onto the next section — which looks like a
    // different piece of UI over the photograph — keep the panel inside the
    // hero and give it its own scrollbar when the list is long.
    const fitDropdown = () => {
      const anchor = $(".search-field", box) || input;
      const rect = anchor.getBoundingClientRect();
      const frame = box.closest(".hero, .detail-hero, .state-hero");
      const gap = 8;
      const breathingRoom = 18;

      // Glue the fixed panel to the field it belongs to.
      dropdown.style.top = `${Math.round(rect.bottom + gap)}px`;
      dropdown.style.left = `${Math.round(rect.left)}px`;
      dropdown.style.width = `${Math.round(rect.width)}px`;

      // Stay inside the hero, and inside the viewport, whichever ends sooner.
      const bottomBound = Math.min(
        frame ? frame.getBoundingClientRect().bottom : Infinity,
        window.innerHeight
      );
      const available = bottomBound - rect.bottom - gap - breathingRoom;
      // Two rows is the smallest list worth showing; 380px is the design cap.
      dropdown.style.maxHeight = `${Math.max(132, Math.min(380, Math.floor(available)))}px`;
    };
    const go = () => {
      const query = input.value.trim();
      if (!query) { input.focus(); return; }
      if (dropdown.classList.contains("open") && active >= 0 && items[active]) {
        window.location.href = items[active].url;
      } else {
        window.location.href = `/place?q=${encodeURIComponent(query)}`;
      }
    };
    if (goButton) goButton.addEventListener("click", go);
    const render = () => {
      if (!items.length) { close(); return; }
      dropdown.innerHTML = items.map((result, index) => `
        <a class="ac-item ${index === active ? "active" : ""}" href="${result.url}">
          <span>
            <span class="ac-name">${escapeHtml(result.name)}</span><br>
            <span class="ac-sub">${escapeHtml(result.subtitle || "")}</span>
          </span>
          <span class="ac-badge ${result.type}">${labelFor(result.type)}</span>
        </a>`).join("");
      dropdown.classList.add("open");
      fitDropdown();
    };

    // Keep the panel correctly sized while the page moves underneath it.
    const refit = () => { if (dropdown.classList.contains("open")) fitDropdown(); };
    window.addEventListener("resize", refit);
    window.addEventListener("scroll", refit, { passive: true });

    input.addEventListener("input", () => {
      const query = input.value.trim();
      clearTimeout(timer);
      if (query.length < 1) { close(); return; }
      timer = setTimeout(() => {
        fetch(`/api/search?q=${encodeURIComponent(query)}&limit=8`)
          .then((response) => response.json())
          .then((data) => { items = data.results || []; active = -1; render(); })
          .catch(close);
      }, 320);
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const query = input.value.trim();
        if (!query) return;
        event.preventDefault();
        if (dropdown.classList.contains("open") && active >= 0 && items[active]) {
          window.location.href = items[active].url;
        } else {
          window.location.href = `/place?q=${encodeURIComponent(query)}`;
        }
        return;
      }
      if (!dropdown.classList.contains("open")) return;
      if (event.key === "ArrowDown") {
        event.preventDefault(); active = Math.min(active + 1, items.length - 1); render();
      } else if (event.key === "ArrowUp") {
        event.preventDefault(); active = Math.max(active - 1, 0); render();
      } else if (event.key === "Escape") close();
    });
    document.addEventListener("click", (event) => { if (!box.contains(event.target)) close(); });
  }
  $$(".search-box").forEach(initSearch);

  const moreButton = $("#view-more-gems");
  if (moreButton) {
    const grid = $("#gems-grid");
    let offset = parseInt(moreButton.dataset.offset || "0", 10);
    moreButton.addEventListener("click", () => {
      moreButton.disabled = true;
      moreButton.textContent = "Loading...";
      fetch(`/api/hidden-gems/${moreButton.dataset.slug}?offset=${offset}&limit=6`)
        .then((response) => response.json())
        .then((data) => {
          (data.results || []).forEach((gem) => grid.insertAdjacentHTML("beforeend", gemCard(gem)));
          offset += (data.results || []).length;
          if (offset >= data.total) moreButton.remove();
          else { moreButton.disabled = false; moreButton.textContent = "View more hidden gems"; }
        })
        .catch(() => { moreButton.disabled = false; moreButton.textContent = "View more hidden gems"; });
    });
  }

  function gemCard(gem) {
    const meta = [gem.region, gem.category].filter(Boolean)
      .map((value) => `<span>${escapeHtml(value)}</span>`).join("");
    const facts = [];
    if (gem.distance_km != null) facts.push(`<span>${gem.distance_km} km away</span>`);
    if (gem.travel_time) facts.push(`<span>${escapeHtml(gem.travel_time)}</span>`);
    if (gem.rating) facts.push(`<span class="rating">Rated ${escapeHtml(gem.rating)}</span>`);
    const href = gem.explore_url ? `href="${gem.explore_url}"` : "";
    const explore = gem.explore_url ? `<a class="btn btn-sm" href="${gem.explore_url}">Explore</a>` : "";
    return `
      <article class="gem-card">
        <a class="gem-thumb" ${href}>
          <span class="badge gem">${escapeHtml(gem.label || "Hidden Gem")}</span>
          <img loading="lazy" src="${gem.image ? mediaUrl(gem.image, gem.name) : placeholder(gem.name)}" alt="${escapeHtml(gem.name)}">
        </a>
        <div class="gem-body">
          <a class="gem-title" ${href}><h3>${escapeHtml(gem.name)}</h3></a>
          <div class="gem-meta">${meta}</div>
          ${gem.description ? `<p class="gem-desc">${escapeHtml(gem.description)}</p>` : ""}
          <div class="gem-facts">${facts.join("")}</div>
          ${gem.why ? `<p class="gem-why">${escapeHtml(gem.why)}</p>` : ""}
          <div class="gem-actions">
            ${explore}
            <a class="btn btn-sm btn-outline directions-link" href="${gem.directions_url}" target="_blank" rel="noopener noreferrer">Get Directions</a>
          </div>
        </div>
      </article>`;
  }

  // ---- Transparent navbar over any page hero, glass on scroll ----
  (function initNavbarOverlay() {
    const nav = $(".navbar");
    const hero = $(".hero.has-media, .detail-hero, .state-hero");
    if (!nav || !hero) return;
    const update = () => {
      const threshold = hero.offsetHeight - nav.offsetHeight - 12;
      nav.classList.toggle("nav-over-hero", window.scrollY < threshold);
    };
    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
  })();

  // ---- Scroll reveal: gentle fade/rise as content enters the viewport ----
  (function initReveal() {
    if (!("IntersectionObserver" in window)) return;
    const els = $$(".card, .gem-card, .visual-card, .visual-tile, .section-head, .feature, .state-gallery-tile");
    if (!els.length) return;
    els.forEach((el) => el.classList.add("reveal"));
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add("in-view"); io.unobserve(entry.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    els.forEach((el) => io.observe(el));
  })();

  // ---- Auto-rotating homepage hero ----
  (function initHero() {
    const hero = $("[data-hero]");
    if (!hero) return;
    const slides = $$("[data-hero-slide]", hero);
    if (slides.length < 1) return;
    const watermark = $("[data-hero-watermark]", hero);
    const focus = $("[data-hero-focus]", hero);
    const focusName = $("[data-hero-focus-name]", hero);
    const dotsWrap = $("[data-hero-dots]", hero);
    let index = 0;
    let timer = null;

    const dots = slides.map((_, i) => {
      if (!dotsWrap) return null;
      const dot = document.createElement("button");
      dot.type = "button";
      dot.className = "hero-dot";
      dot.setAttribute("aria-label", `Destination ${i + 1}`);
      dot.addEventListener("click", () => { show(i); restart(); });
      dotsWrap.appendChild(dot);
      return dot;
    });

    function show(next) {
      index = (next + slides.length) % slides.length;
      slides.forEach((slide, i) => slide.classList.toggle("active", i === index));
      dots.forEach((dot, i) => dot && dot.classList.toggle("active", i === index));
      const slide = slides[index];
      if (watermark && slide.dataset.state) watermark.textContent = slide.dataset.state;
      if (focusName && slide.dataset.name) focusName.textContent = slide.dataset.name;
      if (focus && slide.dataset.url) focus.setAttribute("href", slide.dataset.url);
    }
    function step(delta) { show(index + delta); }
    function restart() { if (timer) clearInterval(timer); if (slides.length > 1) timer = setInterval(() => step(1), 6000); }

    const prev = $("[data-hero-prev]", hero);
    const next = $("[data-hero-next]", hero);
    if (prev) prev.addEventListener("click", () => { step(-1); restart(); });
    if (next) next.addEventListener("click", () => { step(1); restart(); });
    hero.addEventListener("mouseenter", () => { if (timer) clearInterval(timer); });
    hero.addEventListener("mouseleave", restart);

    show(0);
    restart();
  })();

  // ---- Tabbed detail pages (compartments instead of one long scroll) ----
  $$("[data-tabscope]").forEach((scope) => {
    const buttons = $$("[data-tab]", scope);
    const panels = $$("[data-panel]", scope);
    if (!buttons.length || !panels.length) return;

    function activate(key, push) {
      let matched = false;
      buttons.forEach((button) => {
        const on = button.dataset.tab === key;
        button.classList.toggle("active", on);
        button.setAttribute("aria-selected", String(on));
        if (on) matched = true;
      });
      panels.forEach((panel) => { panel.hidden = panel.dataset.panel !== key; });
      if (matched && push && window.history && window.history.replaceState) {
        window.history.replaceState(null, "", `#${key}`);
      }
      return matched;
    }

    buttons.forEach((button) => {
      button.addEventListener("click", () => activate(button.dataset.tab, true));
    });

    const fromHash = (window.location.hash || "").replace("#", "");
    if (!fromHash || !activate(fromHash, false)) {
      activate(buttons[0].dataset.tab, false);
    }

    // The tab strip blends into the page until it actually sticks under the
    // navbar, so it stops reading as a pale box sitting on the content.
    const strip = $("[data-tabs]", scope);
    if (strip && "IntersectionObserver" in window) {
      const sentinel = document.createElement("div");
      sentinel.setAttribute("aria-hidden", "true");
      strip.parentNode.insertBefore(sentinel, strip);
      new IntersectionObserver(
        ([entry]) => strip.classList.toggle("is-stuck", !entry.isIntersecting),
        { rootMargin: "-69px 0px 0px 0px", threshold: 1 }
      ).observe(sentinel);
    }
  });

  // ---- Lightbox for gallery / food / festival / culture photos ----
  (function initLightbox() {
    const triggers = $$("[data-lightbox]");
    if (!triggers.length) return;
    const overlay = document.createElement("div");
    overlay.className = "lightbox";
    overlay.innerHTML = '<button class="lightbox-close" aria-label="Close">Close</button><img alt="">';
    const image = $("img", overlay);
    const closeBtn = $(".lightbox-close", overlay);
    document.body.appendChild(overlay);

    const open = (src, alt) => {
      image.src = src;
      image.alt = alt || "";
      overlay.classList.add("open");
      document.body.style.overflow = "hidden";
    };
    const close = () => {
      overlay.classList.remove("open");
      image.src = "";
      document.body.style.overflow = "";
    };

    triggers.forEach((trigger) => {
      trigger.addEventListener("click", (event) => {
        const src = trigger.getAttribute("href") || trigger.dataset.lightbox;
        if (!src) return;
        event.preventDefault();
        const alt = (trigger.querySelector("img") || {}).alt || "";
        open(src, alt);
      });
    });
    overlay.addEventListener("click", (event) => { if (event.target !== image) close(); });
    closeBtn.addEventListener("click", close);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape") close(); });
  })();

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function labelFor(type) {
    if (type === "external_location") return "Live";
    if (type === "hidden_gem") return "Gem";
    return type === "state" ? "State" : "Place";
  }
  function placeholder(name) { return `/media/placeholder.svg?name=${encodeURIComponent(name || "Atulya Yatra")}`; }
  function mediaUrl(path, name) {
    if (/^https?:\/\//.test(path) || path.startsWith("/")) return path;
    return placeholder(name);
  }
})();
