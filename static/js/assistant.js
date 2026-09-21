/* One-input controller for the floating and full-page travel assistant. */
(function () {
  "use strict";

  const all = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const one = (selector, root = document) => root.querySelector(selector);
  const placeholder = (name) => `/media/placeholder.svg?name=${encodeURIComponent(name || "Atulya Yatra")}`;

  function createNode(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text != null) item.textContent = text;
    return item;
  }

  function appendList(container, values, className = "assistant-guide-list") {
    const clean = (values || []).filter(Boolean);
    if (!clean.length) return;
    const list = createNode("ul", className);
    clean.forEach((value) => {
      const text = typeof value === "string" ? value : [value.name, value.time].filter(Boolean).join(" — ");
      list.appendChild(createNode("li", "", text));
    });
    container.appendChild(list);
  }

  function initAssistant(root) {
    const variant = root.dataset.chatVariant || "floating";
    const body = one("[data-chat-body]", root);
    const options = one("[data-chat-options]", root);
    const status = one("[data-chat-status]", root);
    const form = one("[data-chat-form]", root);
    const input = one("[data-chat-input]", root);
    const submitButton = one("button[type='submit']", form);
    const resetButton = one("[data-chat-reset]", root);
    const closeButton = one("[data-chat-close]", root);
    const fab = variant === "floating" ? one("[data-chat-fab]") : null;
    const storageKey = "atulya-assistant-session";
    let sessionId = sessionStorage.getItem(storageKey) || "";
    let started = false;
    let pending = false;
    let requestNumber = 0;

    function pageContext() {
      const type = root.dataset.contextType || "";
      const slug = root.dataset.contextSlug || "";
      return type && slug ? { type, slug } : null;
    }

    function scroll() {
      body.scrollTop = body.scrollHeight;
    }

    function addMessage(text, sender) {
      if (!text) return;
      body.appendChild(createNode("div", `msg ${sender}`, text));
      scroll();
    }

    function invokeAction(item, display = true) {
      if (pending) return;
      if (item.action === "reset") {
        reset();
        return;
      }
      if (display) addMessage(item.label, "user");
      sendTurn({ action: { type: item.action, value: item.value } });
    }

    function addChoice(item, secondary = false) {
      const button = createNode("button", secondary ? "secondary" : "", item.label);
      button.type = "button";
      button.addEventListener("click", () => invokeAction(item));
      options.appendChild(button);
    }

    function setOptions(data) {
      options.innerHTML = "";
      (data.options || []).forEach((item) => addChoice(item));
      (data.actions || []).forEach((item) => addChoice(item, true));
      (data.suggestions || []).slice(0, 3).forEach((suggestion) => {
        addChoice({ label: suggestion, value: suggestion, action: "answer" }, true);
      });
    }

    function imageElement(place, compact = false) {
      const wrap = createNode("div", compact ? "assistant-image-wrap compact" : "assistant-image-wrap");
      const image = createNode("img", "assistant-place-image");
      image.loading = "lazy";
      image.src = place.image || placeholder(place.name);
      image.alt = place.name ? `${place.name} travel view` : "Travel destination";
      image.addEventListener("error", () => { image.src = placeholder(place.name); }, { once: true });
      wrap.appendChild(image);
      if (place.photo_credit) wrap.appendChild(createNode("small", "assistant-photo-credit", place.photo_credit));
      return wrap;
    }

    function placeCard(place, compact = false) {
      const card = createNode("article", compact ? "assistant-place-card compact" : "assistant-place-card");
      card.appendChild(imageElement(place, compact));
      const content = createNode("div", "assistant-place-content");
      content.appendChild(createNode("div", "assistant-card-eyebrow", place.candidate_type || (place.is_hidden_gem ? "Curated hidden gem" : "Destination")));
      content.appendChild(createNode("h5", "", place.name || "Destination"));
      const subline = [place.place_type, place.state].filter(Boolean).join(" · ");
      if (subline) content.appendChild(createNode("div", "assistant-card-state", subline));
      if (!compact && place.short_intro) content.appendChild(createNode("p", "", place.short_intro));
      if (place.why) content.appendChild(createNode("div", "assistant-why", place.why));
      const facts = [];
      if (place.rating) facts.push(`Rated ${place.rating}`);
      if (place.reviews) facts.push(`${Number(place.reviews).toLocaleString()} reviews`);
      if (place.distance_km != null) facts.push(`${place.distance_km} km away`);
      if (facts.length) content.appendChild(createNode("div", "assistant-card-facts", facts.join(" · ")));
      const actions = createNode("div", "assistant-card-actions");
      if (place.url) {
        const explore = createNode("a", "assistant-link", "View guide");
        explore.href = place.url;
        actions.appendChild(explore);
      }
      if (place.google_maps_url) {
        const maps = createNode("a", "assistant-link secondary", "Open in Maps");
        maps.href = place.google_maps_url;
        maps.target = "_blank";
        maps.rel = "noopener noreferrer";
        actions.appendChild(maps);
      }
      if (place.plan_action && !compact) {
        const plan = createNode("button", "assistant-link secondary", "Plan trip");
        plan.type = "button";
        plan.addEventListener("click", () => invokeAction({ label: `Plan ${place.name}`, action: "plan_place", value: place.slug }));
        actions.appendChild(plan);
      }
      if (actions.children.length) content.appendChild(actions);
      card.appendChild(content);
      return card;
    }

    function locationView(place) {
      const hero = createNode("section", "assistant-location-hero");
      hero.appendChild(imageElement(place));
      const content = createNode("div", "assistant-location-copy");
      content.appendChild(createNode("span", "assistant-location-label", "Your location"));
      content.appendChild(createNode("h3", "", place.name || "Destination"));
      const meta = [place.address, place.state].filter(Boolean).join(" · ");
      if (meta) content.appendChild(createNode("p", "assistant-location-meta", meta));
      const actions = createNode("div", "assistant-card-actions");
      if (place.url) {
        const guide = createNode("a", "assistant-link", "View full guide");
        guide.href = place.url;
        actions.appendChild(guide);
      }
      if (place.google_maps_url) {
        const maps = createNode("a", "assistant-link secondary", "Open in Maps");
        maps.href = place.google_maps_url;
        maps.target = "_blank";
        maps.rel = "noopener noreferrer";
        actions.appendChild(maps);
      }
      if (actions.children.length) content.appendChild(actions);
      hero.appendChild(content);
      body.appendChild(hero);
    }

    function sectionView(section) {
      if (!(section.cards || []).length) return;
      const wrapper = createNode("section", "assistant-result-section");
      const head = createNode("div", "assistant-section-head");
      head.appendChild(createNode("h4", "", section.title || "Places to explore"));
      if (section.subtitle) head.appendChild(createNode("p", "", section.subtitle));
      wrapper.appendChild(head);
      const grid = createNode("div", "assistant-response-grid");
      section.cards.forEach((place) => grid.appendChild(placeCard(place)));
      wrapper.appendChild(grid);
      body.appendChild(wrapper);
    }

    function guideTile(title, value, listValues, wide = false) {
      const hasText = typeof value === "string" && value.trim();
      const hasList = (listValues || []).filter(Boolean).length;
      if (!hasText && !hasList) return null;
      const tile = createNode("article", wide ? "assistant-guide-tile wide" : "assistant-guide-tile");
      tile.appendChild(createNode("h5", "", title));
      if (hasText) tile.appendChild(createNode("p", "", value));
      appendList(tile, listValues);
      return tile;
    }

    function guideView(guide) {
      if (!guide) return;
      const wrapper = createNode("section", "assistant-guide");
      const head = createNode("div", "assistant-section-head");
      head.appendChild(createNode("span", "assistant-location-label", "Trusted state guide"));
      head.appendChild(createNode("h4", "", guide.state_name || "Practical travel guide"));
      if (guide.intro) head.appendChild(createNode("p", "", guide.intro));
      wrapper.appendChild(head);
      if ((guide.visuals || []).length) {
        const visualGrid = createNode("div", "assistant-guide-visuals");
        guide.visuals.forEach((visual) => {
          const figure = createNode("figure", "assistant-guide-visual");
          const image = createNode("img");
          image.loading = "lazy";
          image.src = visual.image || placeholder(visual.title);
          image.alt = visual.title || "Travel culture";
          image.addEventListener("error", () => { image.src = placeholder(visual.title); }, { once: true });
          const caption = createNode("figcaption");
          caption.appendChild(createNode("strong", "", visual.title));
          if (visual.caption) caption.appendChild(createNode("span", "", visual.caption));
          figure.appendChild(image);
          figure.appendChild(caption);
          visualGrid.appendChild(figure);
        });
        wrapper.appendChild(visualGrid);
      }
      const grid = createNode("div", "assistant-guide-grid");
      [
        guideTile("Best time", guide.best_time),
        guideTile("Languages", "", guide.languages),
        guideTile("Food to try", "", guide.food),
        guideTile("Festivals", "", guide.festivals),
        guideTile("Culture and traditions", guide.culture, [], true),
        guideTile("Travel tips", "", guide.travel_tips, true),
        guideTile("Activities", "", guide.activities),
        guideTile("Nature and wildlife", "", guide.wildlife),
        guideTile("Spiritual places", "", guide.spiritual_places),
        guideTile("What to pack", "", guide.packing, true),
        guideTile("General travel safety", "", guide.safety_tips, true),
      ].filter(Boolean).forEach((tile) => grid.appendChild(tile));
      wrapper.appendChild(grid);
      if (guide.guidance_note) wrapper.appendChild(createNode("p", "assistant-guide-note", guide.guidance_note));
      body.appendChild(wrapper);
    }

    function itineraryView(trip) {
      const wrapper = createNode("section", "assistant-itinerary");
      wrapper.appendChild(createNode("h4", "", trip.title || "Your itinerary"));
      const meta = [trip.style, trip.traveller, trip.budget].filter(Boolean).join(" · ");
      if (meta) wrapper.appendChild(createNode("div", "assistant-trip-meta", meta));
      if (trip.route_note) wrapper.appendChild(createNode("p", "assistant-route-note", trip.route_note));
      (trip.days || []).forEach((day) => {
        const details = createNode("details", "assistant-day");
        if (day.day === 1 || variant === "page") details.open = true;
        const summary = createNode("summary");
        summary.appendChild(createNode("span", "assistant-day-number", `Day ${day.day}`));
        summary.appendChild(createNode("strong", "", day.title || "Flexible day"));
        details.appendChild(summary);
        const content = createNode("div", "assistant-day-content");
        if (day.place) content.appendChild(placeCard(day.place, true));
        [
          ["Morning", day.morning], ["Afternoon", day.afternoon], ["Evening", day.evening],
          ["Travel", day.travel], ["Stay", day.stay], ["Safety", day.safety], ["Etiquette", day.etiquette],
        ].forEach(([label, value]) => {
          if (!value) return;
          const row = createNode("div", "assistant-agenda-row");
          row.appendChild(createNode("b", "", label));
          row.appendChild(createNode("span", "", value));
          content.appendChild(row);
        });
        if (day.food && day.food.name) {
          const food = createNode("div", "assistant-food");
          food.appendChild(createNode("b", "", "Local food from our data"));
          food.appendChild(createNode("span", "", `${day.food.name}${day.food.description ? ` — ${day.food.description}` : ""}`));
          content.appendChild(food);
        }
        if (day.hidden_gem) {
          content.appendChild(createNode("b", "assistant-detour-label", "Optional curated detour"));
          content.appendChild(placeCard(day.hidden_gem, true));
        }
        details.appendChild(content);
        wrapper.appendChild(details);
      });
      body.appendChild(wrapper);
    }

    function render(data) {
      addMessage(data.message, "bot");
      if (data.location) locationView(data.location);
      // Sections that accompany an itinerary read as follow-on suggestions, so
      // they are held back until after the plan is drawn.
      const sections = data.sections || [];
      const leading = data.itinerary ? [] : sections;
      data.trailingSections = data.itinerary ? sections : [];
      leading.forEach(sectionView);
      guideView(data.guide);
      if ((data.cards || []).length) {
        const grid = createNode("div", "assistant-response-grid");
        data.cards.forEach((place) => grid.appendChild(placeCard(place)));
        body.appendChild(grid);
      }
      if (data.itinerary) itineraryView(data.itinerary);
      // "You can also check out" belongs after the plan it follows on from.
      (data.trailingSections || []).forEach(sectionView);
      if (data.disclaimer) body.appendChild(createNode("p", "assistant-disclaimer", data.disclaimer));
      setOptions(data);
      scroll();
    }

    function setPending(value) {
      pending = value;
      input.disabled = value;
      submitButton.disabled = value;
      status.textContent = value ? "Finding live places and trusted travel details..." : "";
      options.classList.toggle("disabled", value);
    }

    function sendTurn({ message = "", action = null, mode = null } = {}) {
      if (pending) return;
      const thisRequest = ++requestNumber;
      setPending(true);
      fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId || null, message, action, mode, context: pageContext() }),
      })
        .then((response) => {
          if (!response.ok) throw new Error(`Assistant request failed (${response.status})`);
          return response.json();
        })
        .then((data) => {
          if (thisRequest !== requestNumber) return;
          sessionId = data.session_id || sessionId;
          if (sessionId) sessionStorage.setItem(storageKey, sessionId);
          render(data);
        })
        .catch(() => {
          if (thisRequest === requestNumber) addMessage("I could not complete that request. Please try the location name again.", "bot error");
        })
        .finally(() => {
          if (thisRequest === requestNumber) {
            setPending(false);
            input.focus();
          }
        });
    }

    function open(initialize = true) {
      root.classList.add("open");
      if (fab) fab.setAttribute("aria-expanded", "true");
      if (!started) {
        started = true;
        if (initialize) sendTurn();
      } else input.focus();
    }

    function close() {
      root.classList.remove("open");
      if (fab) {
        fab.setAttribute("aria-expanded", "false");
        fab.focus();
      }
    }

    function reset() {
      requestNumber += 1;
      pending = false;
      setPending(false);
      const oldSession = sessionId;
      sessionId = "";
      sessionStorage.removeItem(storageKey);
      body.innerHTML = "";
      options.innerHTML = "";
      fetch("/api/chat/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: oldSession }),
      }).finally(() => sendTurn());
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message || pending) return;
      addMessage(message, "user");
      input.value = "";
      sendTurn({ message });
    });
    if (fab) fab.addEventListener("click", () => root.classList.contains("open") ? close() : open());
    if (closeButton) closeButton.addEventListener("click", close);
    resetButton.addEventListener("click", reset);

    if (variant === "page") {
      root.classList.add("open");
      started = true;
      // A destination page can hand its place over via ?plan=<slug>. The
      // `plan_place` action puts the assistant straight on "how many days?"
      // for that place, so the visitor never has to retype where they are going.
      const planSlug = root.dataset.planSlug || "";
      const planQuery = root.dataset.planQuery || "";
      const planLabel = root.dataset.planLabel || "";
      if (planSlug) {
        addMessage(planLabel ? `Plan a trip to ${planLabel}` : "Plan a trip here", "user");
        sendTurn({ action: { type: "plan_place", value: planSlug } });
      } else if (planQuery) {
        // Live, non-curated places have no slug, so the name is resolved instead.
        addMessage(`Plan a trip to ${planQuery}`, "user");
        sendTurn({ message: `Plan a trip to ${planQuery}` });
      } else {
        sendTurn();
      }
    }

    return {
      launch(mode, slug) {
        root.dataset.contextType = "destination";
        root.dataset.contextSlug = slug || root.dataset.contextSlug;
        open(false);
        const prompt = mode === "plan" ? "Plan my trip here" : "Show places and hidden gems nearby";
        addMessage(prompt, "user");
        sendTurn({ action: { type: "set_mode", value: mode } });
      },
    };
  }

  const controllers = new Map();
  all("[data-chat-root]").forEach((root) => controllers.set(root, initAssistant(root)));
  all(".assistant-launch").forEach((button) => button.addEventListener("click", () => {
    const root = one('[data-chat-root][data-chat-variant="floating"]');
    const controller = root ? controllers.get(root) : null;
    if (controller) controller.launch(button.dataset.assistantMode, button.dataset.contextSlug);
  }));
})();
