(function () {
  'use strict';

  // Reveal-on-scroll animation
  const revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && revealEls.length) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.12 });
    revealEls.forEach((el) => io.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add('in'));
  }

  // Scroll-to-top button
  const btn = document.getElementById('scrollTopBtn');
  if (btn) {
    window.addEventListener('scroll', () => {
      btn.classList.toggle('show', window.scrollY > 600);
    });
    btn.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  // Smooth focus on hash navigation
  document.querySelectorAll('a[href^="#"]').forEach((a) => {
    a.addEventListener('click', (e) => {
      const id = a.getAttribute('href');
      if (id && id.length > 1) {
        const target = document.querySelector(id);
        if (target) {
          e.preventDefault();
          target.scrollIntoView({ behavior: 'smooth' });
        }
      }
    });
  });

  // Lazy table-row entrance
  const rows = document.querySelectorAll('.gg-table tbody tr');
  rows.forEach((row, i) => {
    row.style.opacity = '0';
    row.style.transform = 'translateY(6px)';
    row.style.transition = 'opacity .3s ease, transform .3s ease';
    setTimeout(() => {
      row.style.opacity = '1';
      row.style.transform = 'translateY(0)';
    }, Math.min(i * 18, 400));
  });

  // ----------------------------------------------------------------
  // Display settings (3-dot menu): persists to localStorage and toggles
  // body classes that the CSS uses to hide/show odds and table positions.
  // ----------------------------------------------------------------
  (function initDisplaySettings() {
    const STORAGE_KEY = 'dk-display-settings-v1';
    const defaults = { 'show-odds': true, 'show-positions': false };
    let state = { ...defaults };
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) state = { ...defaults, ...JSON.parse(raw) };
    } catch (_) { /* ignore */ }

    const apply = () => {
      document.body.classList.toggle('hide-odds', !state['show-odds']);
      document.body.classList.toggle('show-positions', !!state['show-positions']);
    };
    apply();

    document.querySelectorAll('input[data-setting]').forEach((el) => {
      const key = el.dataset.setting;
      if (!(key in state)) return;
      el.checked = !!state[key];
      el.addEventListener('change', () => {
        state[key] = el.checked;
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (_) {}
        apply();
      });
    });
  })();

  // ----------------------------------------------------------------
  // Featured-leagues hide toggles (3-dot menu): user can hide entire
  // competitions (UCL, Conference, etc.) from the featured-fixtures
  // carousel and any other fx-card grid on the page. Persists to
  // localStorage so the choice sticks across reloads.
  // ----------------------------------------------------------------
  (function initLeagueToggles() {
    const STORAGE_KEY = 'dk-hidden-leagues-v1';
    let hidden = new Set();
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) hidden = new Set(arr.map(String));
      }
    } catch (_) { /* ignore */ }

    const apply = () => {
      document.querySelectorAll('.fx-card[data-comp-code]').forEach((card) => {
        const code = (card.dataset.compCode || '').toUpperCase();
        const hide = code && hidden.has(code);
        card.classList.toggle('d-none', !!hide);
        // If the card sits in a carousel item, hide the wrapper too so the
        // grid does not leave an empty slot.
        const wrapper = card.closest('.dk-carousel-item');
        if (wrapper) wrapper.classList.toggle('dk-league-hidden', !!hide);
      });
      // Stats hub cards (top scorers/assists/cards) — same toggle.
      document.querySelectorAll('.dk-stat-card[data-comp-code]').forEach((card) => {
        const code = (card.dataset.compCode || '').toUpperCase();
        const hide = code && hidden.has(code);
        card.classList.toggle('is-league-hidden', !!hide);
      });
    };

    document.querySelectorAll('input[data-hide-league]').forEach((el) => {
      const code = (el.dataset.hideLeague || '').toUpperCase();
      if (!code) return;
      el.checked = hidden.has(code);
      el.addEventListener('change', () => {
        if (el.checked) hidden.add(code); else hidden.delete(code);
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify([...hidden])); } catch (_) {}
        apply();
      });
    });

    apply();
  })();

  // Horizontal carousel progress dashes (.dk-carousel)
  const carousels = document.querySelectorAll('.dk-carousel[data-carousel]');
  carousels.forEach((root) => {
    const track = root.querySelector('.dk-carousel-track');
    const progress = root.querySelector('.dk-carousel-progress');
    const bar = root.querySelector('.dk-carousel-progress-bar');
    if (!track) return;

    const updateProgress = () => {
      if (!progress || !bar) return;
      const max = Math.max(0, track.scrollWidth - track.clientWidth);
      const overflow = max > 2;
      progress.hidden = !overflow;
      if (!overflow) {
        bar.style.width = '100%';
        bar.style.transform = 'translateX(0)';
        return;
      }
      const railWidth = progress.clientWidth || 120;
      const thumbWidth = Math.max(56, Math.min(railWidth, railWidth * (track.clientWidth / track.scrollWidth)));
      const maxLeft = Math.max(0, railWidth - thumbWidth);
      const left = max <= 0 ? 0 : (track.scrollLeft / max) * maxLeft;
      bar.style.width = `${thumbWidth}px`;
      bar.style.transform = `translateX(${left}px)`;
    };

    if (progress) {
      progress.addEventListener('click', (event) => {
        const rect = progress.getBoundingClientRect();
        if (!rect.width) return;
        const pct = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        const max = Math.max(0, track.scrollWidth - track.clientWidth);
        track.scrollTo({ left: max * pct, behavior: 'smooth' });
      });
    }

    track.addEventListener('scroll', updateProgress, { passive: true });
    window.addEventListener('resize', updateProgress);
    // Wait one frame so layout (including images) settles.
    requestAnimationFrame(updateProgress);
    setTimeout(updateProgress, 250);
  });
})();

// ----------------------------------------------------------------
// Live search autocomplete (typeahead) — hooks any [data-autocomplete]
// wrapper that contains a [data-autocomplete-input] and a sibling
// [data-autocomplete-panel]. Hits /api/search?q=… (debounced).
// ----------------------------------------------------------------
(function initAutocomplete() {
  const wraps = document.querySelectorAll('[data-autocomplete]');
  if (!wraps.length) return;

  const cache = new Map();
  const escape = (s) => String(s || '').replace(/[&<>"']/g, (c) => (
    { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]
  ));

  function competitionUrl(code) { return code ? `/competition/${encodeURIComponent(code)}` : '#'; }
  function teamUrl(id) { return id ? `/team/${id}` : '#'; }
  function playerUrl(id) { return id ? `/player/${id}` : '#'; }

  function renderResults(results, q) {
    const { competitions = [], teams = [], players = [] } = results || {};
    const total = competitions.length + teams.length + players.length;
    if (!total) {
      return `<div class="dk-search-suggest-empty"><i class="bi bi-search me-2"></i>No matches for "<strong>${escape(q)}</strong>"</div>`;
    }

    let html = '';

    if (competitions.length) {
      html += `<div class="dk-search-suggest-section"><div class="dk-search-suggest-title"><i class="bi bi-trophy"></i>Competitions</div>`;
      competitions.forEach((c) => {
        html += `
          <a class="dk-search-suggest-item" href="${competitionUrl(c.code)}">
            <span class="dk-search-suggest-emblem">${c.emblem ? `<img src="${escape(c.emblem)}" alt="" />` : '<i class="bi bi-trophy"></i>'}</span>
            <span class="dk-search-suggest-text">
              <span class="dk-search-suggest-name">${escape(c.name || '')}</span>
              <span class="dk-search-suggest-meta">${escape(c.country || '')}${c.code ? ' · ' + escape(c.code) : ''}</span>
            </span>
          </a>`;
      });
      html += '</div>';
    }

    if (teams.length) {
      html += `<div class="dk-search-suggest-section"><div class="dk-search-suggest-title"><i class="bi bi-people"></i>Teams</div>`;
      teams.forEach((t) => {
        const lg = (t.league && t.league.name) || '';
        html += `
          <a class="dk-search-suggest-item" href="${teamUrl(t.id)}">
            <span class="dk-search-suggest-emblem">${t.crest ? `<img src="${escape(t.crest)}" alt="" />` : '<i class="bi bi-shield"></i>'}</span>
            <span class="dk-search-suggest-text">
              <span class="dk-search-suggest-name">${escape(t.shortName || t.name || '')}</span>
              <span class="dk-search-suggest-meta">${escape(lg)}${t.tla ? ' · ' + escape(t.tla) : ''}</span>
            </span>
          </a>`;
      });
      html += '</div>';
    }

    if (players.length) {
      html += `<div class="dk-search-suggest-section"><div class="dk-search-suggest-title"><i class="bi bi-person-arms-up"></i>Players</div>`;
      players.forEach((p) => {
        const tn = (p.team && (p.team.shortName || p.team.name)) || '';
        const lg = (p.league && p.league.name) || '';
        html += `
          <a class="dk-search-suggest-item" href="${playerUrl(p.id)}">
            <span class="dk-search-suggest-emblem">${p.team && p.team.crest ? `<img src="${escape(p.team.crest)}" alt="" />` : '<i class="bi bi-person-fill"></i>'}</span>
            <span class="dk-search-suggest-text">
              <span class="dk-search-suggest-name">${escape(p.name || '')}</span>
              <span class="dk-search-suggest-meta">${escape(tn)}${lg ? ' · ' + escape(lg) : ''}${p.position ? ' · ' + escape(p.position) : ''}</span>
            </span>
          </a>`;
      });
      html += '</div>';
    }

    html += `<div class="dk-search-suggest-footer"><span>${total} match${total === 1 ? '' : 'es'}</span><span><i class="bi bi-arrow-return-left me-1"></i>Enter for full results</span></div>`;
    return html;
  }

  wraps.forEach((wrap) => {
    const input = wrap.querySelector('[data-autocomplete-input]');
    const panel = wrap.querySelector('[data-autocomplete-panel]');
    if (!input || !panel) return;

    let timer = null;
    let activeRequest = 0;
    let activeIndex = -1;

    function close() {
      panel.hidden = true;
      activeIndex = -1;
    }

    function open() {
      panel.hidden = false;
    }

    function highlight() {
      const items = panel.querySelectorAll('.dk-search-suggest-item');
      items.forEach((it, i) => it.classList.toggle('is-active', i === activeIndex));
      const active = items[activeIndex];
      if (active) active.scrollIntoView({ block: 'nearest' });
    }

    function fetchSuggestions(q) {
      const reqId = ++activeRequest;
      if (cache.has(q)) {
        const cached = cache.get(q);
        if (reqId === activeRequest) {
          panel.innerHTML = renderResults(cached, q);
          open();
        }
        return;
      }
      panel.innerHTML = `<div class="dk-search-suggest-loading"><span class="spinner"></span>Searching…</div>`;
      open();
      fetch(`/api/search?q=${encodeURIComponent(q)}&limit=5`, { credentials: 'same-origin' })
        .then((r) => r.ok ? r.json() : Promise.reject(r))
        .then((data) => {
          cache.set(q, data);
          if (reqId === activeRequest) {
            panel.innerHTML = renderResults(data, q);
            open();
          }
        })
        .catch(() => {
          if (reqId === activeRequest) {
            panel.innerHTML = `<div class="dk-search-suggest-empty"><i class="bi bi-exclamation-triangle me-2"></i>Search is temporarily unavailable.</div>`;
            open();
          }
        });
    }

    input.addEventListener('input', () => {
      const q = input.value.trim();
      if (timer) clearTimeout(timer);
      activeIndex = -1;
      if (q.length < 2) {
        close();
        return;
      }
      timer = setTimeout(() => fetchSuggestions(q), 180);
    });

    input.addEventListener('focus', () => {
      const q = input.value.trim();
      if (q.length >= 2) fetchSuggestions(q);
    });

    input.addEventListener('keydown', (e) => {
      const items = panel.querySelectorAll('.dk-search-suggest-item');
      if (e.key === 'ArrowDown' && items.length) {
        e.preventDefault();
        activeIndex = (activeIndex + 1) % items.length;
        highlight();
      } else if (e.key === 'ArrowUp' && items.length) {
        e.preventDefault();
        activeIndex = (activeIndex - 1 + items.length) % items.length;
        highlight();
      } else if (e.key === 'Enter' && activeIndex >= 0 && items[activeIndex]) {
        e.preventDefault();
        window.location.href = items[activeIndex].getAttribute('href');
      } else if (e.key === 'Escape') {
        close();
        input.blur();
      }
    });

    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) close();
    });
  });
})();

// ----------------------------------------------------------------
// Compare pickers — DB-backed typeahead for teams and players.
// Keeps compare pages light even with large offline archives.
// ----------------------------------------------------------------
(function initComparePickers() {
  const forms = document.querySelectorAll('[data-compare-form]');
  if (!forms.length) return;

  const cache = new Map();
  const escape = (s) => String(s || '').replace(/[&<>"']/g, (c) => (
    { '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]
  ));

  function normalizeKind(kind) {
    return (kind === 'player' ? 'players' : (kind === 'team' ? 'teams' : kind)) || 'teams';
  }

  function kindLabel(kind) {
    return kind === 'players' ? 'players' : 'teams';
  }

  function defaultHint(kind) {
    return kind === 'players'
      ? 'Search from your offline player archive.'
      : 'Search from your offline team archive.';
  }

  function resultItems(data, kind) {
    if (kind === 'players') return (data && data.players) || [];
    return (data && data.teams) || [];
  }

  function previewMarkup(item, kind) {
    var img = item.img || '';
    var subtitle = item.subtitle || '';
    var fallback = kind === 'players' ? '<i class="bi bi-person-fill"></i>' : '<i class="bi bi-shield"></i>';
    return '' +
      '<span class="dk-cmp-preview-thumb">' +
        (img ? '<img src="' + escape(img) + '" alt=""/>' : fallback) +
      '</span>' +
      '<div class="dk-cmp-preview-meta">' +
        '<div class="dk-cmp-preview-name">' + escape(item.name || '') + '</div>' +
        '<div class="dk-cmp-preview-sub">' + escape(subtitle || defaultHint(kind)) + '</div>' +
      '</div>';
  }

  function renderChoice(item, kind) {
    if (kind === 'players') {
      const team = (item.team && (item.team.shortName || item.team.name)) || '';
      const league = (item.league && item.league.name) || '';
      const img = item._photo_local || item._photo || (item.team && item.team.crest) || '';
      const meta = [team, league, item.position || ''].filter(Boolean).join(' · ');
      return '' +
        `<button type="button" class="dk-search-suggest-item" data-compare-choice data-id="${escape(item.id)}" data-name="${escape(item.name || '')}" data-team="${escape(team)}" data-league="${escape(league)}" data-img="${escape(img)}" data-subtitle="${escape(meta)}">` +
          `<span class="dk-search-suggest-emblem">${img ? `<img src="${escape(img)}" alt="" />` : '<i class="bi bi-person-fill"></i>'}</span>` +
          `<span class="dk-search-suggest-text">` +
            `<span class="dk-search-suggest-name">${escape(item.name || '')}</span>` +
            `<span class="dk-search-suggest-meta">${escape(meta)}</span>` +
          `</span>` +
        `</button>`;
    }

    const league = (item.league && item.league.name) || item._competition || '';
    const meta = [league, item.tla || ''].filter(Boolean).join(' · ');
    return '' +
      `<button type="button" class="dk-search-suggest-item" data-compare-choice data-id="${escape(item.id)}" data-name="${escape(item.shortName || item.name || '')}" data-team="${escape(item.name || '')}" data-league="${escape(league)}" data-img="${escape(item.crest || '')}" data-subtitle="${escape(meta || item.name || '')}">` +
        `<span class="dk-search-suggest-emblem">${item.crest ? `<img src="${escape(item.crest)}" alt="" />` : '<i class="bi bi-shield"></i>'}</span>` +
        `<span class="dk-search-suggest-text">` +
          `<span class="dk-search-suggest-name">${escape(item.shortName || item.name || '')}</span>` +
          `<span class="dk-search-suggest-meta">${escape(meta || item.name || '')}</span>` +
        `</span>` +
      `</button>`;
  }

  function renderResults(items, q, kind) {
    if (!items.length) {
      return `<div class="dk-search-suggest-empty"><i class="bi bi-search me-2"></i>No cached ${kindLabel(kind)} for "<strong>${escape(q)}</strong>"</div>`;
    }
    const title = kind === 'players' ? 'Players' : 'Teams';
    return '' +
      `<div class="dk-search-suggest-section">` +
        `<div class="dk-search-suggest-title"><i class="bi ${kind === 'players' ? 'bi-person-arms-up' : 'bi-people'}"></i>${title}</div>` +
        items.map((item) => renderChoice(item, kind)).join('') +
      `</div>`;
  }

  forms.forEach((form) => {
    const pickers = Array.from(form.querySelectorAll('[data-compare-picker]'));
    if (!pickers.length) return;

    pickers.forEach((picker) => {
      const kind = normalizeKind(picker.getAttribute('data-kind') || form.getAttribute('data-kind') || 'teams');
      const input = picker.querySelector('[data-compare-input]');
      const hidden = picker.querySelector('[data-compare-id]');
      const panel = picker.querySelector('[data-compare-panel]');
      const hint = picker.parentElement ? picker.parentElement.querySelector('[data-compare-hint]') : null;
      const preview = picker.parentElement ? picker.parentElement.querySelector('[data-compare-preview]') : null;
      if (!input || !hidden || !panel) return;

      let timer = null;
      let activeRequest = 0;
      let activeIndex = -1;

      function close() {
        panel.hidden = true;
        activeIndex = -1;
      }

      function open() {
        panel.hidden = false;
      }

      function selectedHint(item) {
        const league = item.league || '';
        const team = item.team || '';
        if (kind === 'players') return [item.name, team].filter(Boolean).join(' · ') || defaultHint(kind);
        return [team || item.name, league].filter(Boolean).join(' · ') || defaultHint(kind);
      }

      function updatePreview(item) {
        if (!preview) return;
        if (!item || !item.name) {
          preview.innerHTML = `<div class="dk-cmp-preview-empty">${defaultHint(kind)}</div>`;
          return;
        }
        preview.innerHTML = previewMarkup(item, kind);
      }

      function highlight() {
        const items = panel.querySelectorAll('[data-compare-choice]');
        items.forEach((it, i) => it.classList.toggle('is-active', i === activeIndex));
        const active = items[activeIndex];
        if (active) active.scrollIntoView({ block: 'nearest' });
      }

      function maybeSubmit() {
        const ready = pickers.every((node) => {
          const hid = node.querySelector('[data-compare-id]');
          return hid && hid.value;
        });
        if (ready) {
          if (typeof form.requestSubmit === 'function') form.requestSubmit();
          else form.submit();
        }
      }

      function setSelection(item) {
        hidden.value = item.id || '';
        input.value = item.name || '';
        input.dataset.selectedValue = item.name || '';
        if (hint) hint.textContent = selectedHint(item);
        updatePreview(item);
        close();
        maybeSubmit();
      }

      function fetchSuggestions(q) {
        const cacheKey = `${kind}:${q.toLowerCase()}`;
        const reqId = ++activeRequest;
        if (cache.has(cacheKey)) {
          const cached = cache.get(cacheKey);
          panel.innerHTML = renderResults(cached, q, kind);
          open();
          return;
        }
        panel.innerHTML = `<div class="dk-search-suggest-loading"><span class="spinner"></span>Searching cached ${kindLabel(kind)}...</div>`;
        open();
        fetch(`/api/search?q=${encodeURIComponent(q)}&limit=8&kind=${encodeURIComponent(kind)}`, { credentials: 'same-origin' })
          .then((r) => r.ok ? r.json() : Promise.reject(r))
          .then((data) => {
            if (reqId !== activeRequest) return;
            const items = resultItems(data, kind);
            cache.set(cacheKey, items);
            panel.innerHTML = renderResults(items, q, kind);
            open();
          })
          .catch(() => {
            if (reqId !== activeRequest) return;
            panel.innerHTML = `<div class="dk-search-suggest-empty"><i class="bi bi-exclamation-triangle me-2"></i>Cached search is temporarily unavailable.</div>`;
            open();
          });
      }

      input.addEventListener('input', () => {
        const q = input.value.trim();
        if ((input.dataset.selectedValue || '') !== q) hidden.value = '';
        if (!q) {
          if (hint) hint.textContent = defaultHint(kind);
          if (preview) updatePreview(null);
        }
        if (timer) clearTimeout(timer);
        activeIndex = -1;
        if (q.length < 2) {
          close();
          return;
        }
        timer = setTimeout(() => fetchSuggestions(q), 160);
      });

      input.addEventListener('focus', () => {
        const q = input.value.trim();
        if (q.length >= 2) fetchSuggestions(q);
      });

      input.addEventListener('keydown', (e) => {
        const items = panel.querySelectorAll('[data-compare-choice]');
        if (e.key === 'ArrowDown' && items.length) {
          e.preventDefault();
          activeIndex = (activeIndex + 1) % items.length;
          highlight();
        } else if (e.key === 'ArrowUp' && items.length) {
          e.preventDefault();
          activeIndex = (activeIndex - 1 + items.length) % items.length;
          highlight();
        } else if (e.key === 'Enter' && activeIndex >= 0 && items[activeIndex]) {
          e.preventDefault();
          items[activeIndex].click();
        } else if (e.key === 'Escape') {
          close();
          input.blur();
        }
      });

      panel.addEventListener('mousedown', (e) => {
        const choice = e.target.closest('[data-compare-choice]');
        if (!choice) return;
        e.preventDefault();
        setSelection({
          id: choice.getAttribute('data-id') || '',
          name: choice.getAttribute('data-name') || '',
          team: choice.getAttribute('data-team') || '',
          league: choice.getAttribute('data-league') || '',
          img: choice.getAttribute('data-img') || '',
          subtitle: choice.getAttribute('data-subtitle') || '',
        });
      });

      document.addEventListener('click', (e) => {
        if (!picker.contains(e.target)) close();
      });
    });

    var swapBtn = form.querySelector('[data-compare-swap]');
    if (swapBtn && pickers.length >= 2) {
      swapBtn.addEventListener('click', function(){
        var first = pickers[0];
        var second = pickers[1];
        var firstInput = first.querySelector('[data-compare-input]');
        var secondInput = second.querySelector('[data-compare-input]');
        var firstHidden = first.querySelector('[data-compare-id]');
        var secondHidden = second.querySelector('[data-compare-id]');
        var firstHint = first.parentElement ? first.parentElement.querySelector('[data-compare-hint]') : null;
        var secondHint = second.parentElement ? second.parentElement.querySelector('[data-compare-hint]') : null;
        var firstPreview = first.parentElement ? first.parentElement.querySelector('[data-compare-preview]') : null;
        var secondPreview = second.parentElement ? second.parentElement.querySelector('[data-compare-preview]') : null;
        var firstMarkup = firstPreview ? firstPreview.innerHTML : '';
        var secondMarkup = secondPreview ? secondPreview.innerHTML : '';
        var firstHintText = firstHint ? firstHint.textContent : '';
        var secondHintText = secondHint ? secondHint.textContent : '';
        var firstVal = { id: firstHidden.value, input: firstInput.value, selected: firstInput.dataset.selectedValue || '' };
        var secondVal = { id: secondHidden.value, input: secondInput.value, selected: secondInput.dataset.selectedValue || '' };

        firstHidden.value = secondVal.id;
        firstInput.value = secondVal.input;
        firstInput.dataset.selectedValue = secondVal.selected;
        secondHidden.value = firstVal.id;
        secondInput.value = firstVal.input;
        secondInput.dataset.selectedValue = firstVal.selected;
        if (firstHint) firstHint.textContent = secondHintText;
        if (secondHint) secondHint.textContent = firstHintText;
        if (firstPreview) firstPreview.innerHTML = secondMarkup;
        if (secondPreview) secondPreview.innerHTML = firstMarkup;
        if (firstHidden.value && secondHidden.value) {
          if (typeof form.requestSubmit === 'function') form.requestSubmit();
          else form.submit();
        }
      });
    }
  });
})();

// ----------------------------------------------------------------
// Live "Today" countdown for upcoming fixture cards.
// Replaces the static date+time on any element with [data-fx-date],
// [data-fx-time] or [data-fx-pill] when its UTC kickoff falls on the
// user's local "today". Tick every 30 seconds.
// ----------------------------------------------------------------
(function initFixtureCountdown() {
  const dateEls = document.querySelectorAll('[data-utc][data-fx-date]');
  const timeEls = document.querySelectorAll('[data-utc][data-fx-time]');
  const pillEls = document.querySelectorAll('[data-utc][data-fx-pill]');
  if (!dateEls.length && !timeEls.length && !pillEls.length) return;

  // Cache original innerHTML so toggling away from "today" restores it.
  const cache = new WeakMap();
  function snap(el) { if (!cache.has(el)) cache.set(el, el.innerHTML); }
  function restore(el) { if (cache.has(el)) el.innerHTML = cache.get(el); }

  function fmtCountdown(diffMs) {
    if (diffMs <= 0) return 'Live soon';
    const totalMin = Math.floor(diffMs / 60000);
    if (totalMin < 1) return 'Soon';
    if (totalMin < 60) return totalMin + 'm';
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    if (h < 24) return m ? (h + 'h ' + m + 'm') : (h + 'h');
    const d = Math.floor(h / 24);
    return d + 'd ' + (h % 24) + 'h';
  }

  function isSameLocalDay(a, b) {
    return a.getFullYear() === b.getFullYear()
        && a.getMonth()    === b.getMonth()
        && a.getDate()     === b.getDate();
  }

  function tick() {
    const now = new Date();

    dateEls.forEach((el) => {
      const utc = el.dataset.utc; if (!utc) return;
      const ko = new Date(utc); if (isNaN(ko)) return;
      snap(el);
      if (isSameLocalDay(now, ko)) {
        el.classList.add('is-today');
        el.textContent = 'Today';
      } else {
        el.classList.remove('is-today');
        restore(el);
      }
    });

    timeEls.forEach((el) => {
      const utc = el.dataset.utc; if (!utc) return;
      const ko = new Date(utc); if (isNaN(ko)) return;
      snap(el);
      if (isSameLocalDay(now, ko)) {
        el.classList.add('fx-time-countdown');
        el.textContent = fmtCountdown(ko - now);
      } else {
        el.classList.remove('fx-time-countdown');
        restore(el);
      }
    });

    pillEls.forEach((el) => {
      const utc = el.dataset.utc; if (!utc) return;
      const ko = new Date(utc); if (isNaN(ko)) return;
      snap(el);
      if (isSameLocalDay(now, ko)) {
        el.classList.add('hero-fx-time-countdown');
        el.innerHTML = '<i class="bi bi-broadcast me-1"></i>Today · ' + fmtCountdown(ko - now);
      } else {
        el.classList.remove('hero-fx-time-countdown');
        restore(el);
      }
    });
  }

  tick();
  setInterval(tick, 30000);
})();

/* Top-of-the-table standings carousel — prev/next arrows */
(function () {
  const stage = document.querySelector('[data-qual-stage]');
  if (!stage) return;
  const slides = Array.from(stage.querySelectorAll('[data-qual-slide]'));
  const counter = document.querySelector('[data-qual-counter]');
  const prev = document.querySelector('[data-qual-prev]');
  const next = document.querySelector('[data-qual-next]');
  if (!slides.length) return;
  let idx = 0;
  function show(n) {
    idx = (n + slides.length) % slides.length;
    slides.forEach((s, i) => s.classList.toggle('is-active', i === idx));
    if (counter) counter.textContent = `${idx + 1} / ${slides.length}`;
  }
  if (prev) prev.addEventListener('click', () => show(idx - 1));
  if (next) next.addEventListener('click', () => show(idx + 1));
  document.addEventListener('keydown', (e) => {
    if (!stage.closest('section').matches(':hover, :focus-within')) return;
    if (e.key === 'ArrowLeft') show(idx - 1);
    if (e.key === 'ArrowRight') show(idx + 1);
  });
})();

/* ---------- Match betting markets controller ---------- */
(function () {
  const panels = document.querySelectorAll('[data-bet-panel]');
  if (!panels.length) return;

  function fmtPrice(p) {
    if (p == null || p === '' || isNaN(p)) return '—';
    return Number(p).toFixed(2);
  }

  function applyFilter(panel, bookmaker) {
    panel.querySelectorAll('.dk-bet-price').forEach((el) => {
      const bestVal = el.dataset.best;
      const bestBook = el.dataset.bestBook || '';
      let rows = [];
      try { rows = JSON.parse(el.dataset.rows || '[]'); } catch (e) { rows = []; }
      const bkEl = el.parentElement.querySelector('.dk-bet-bk');
      const cell = el.closest('.dk-bet-cell');
      const outcome = cell ? cell.dataset.outcome : null;
      const key = el.dataset.key || outcome;
      if (bookmaker === '__best__') {
        if (bestVal) {
          el.textContent = fmtPrice(bestVal);
          el.classList.add('is-best');
          el.classList.remove('is-na');
          if (bkEl) bkEl.textContent = bestBook ? '@ ' + bestBook : '';
        } else {
          el.textContent = '—';
          el.classList.add('is-na');
          el.classList.remove('is-best');
          if (bkEl) bkEl.textContent = '';
        }
      } else {
        const row = rows.find((r) => r.bookmaker === bookmaker);
        const v = row && key ? row[key] : null;
        if (v != null) {
          el.textContent = fmtPrice(v);
          el.classList.remove('is-best', 'is-na');
          if (bkEl) bkEl.textContent = '@ ' + bookmaker;
        } else {
          el.textContent = 'n/a';
          el.classList.add('is-na');
          el.classList.remove('is-best');
          if (bkEl) bkEl.textContent = bookmaker + ' — not offered';
        }
      }
    });
  }

  panels.forEach((panel) => {
    const filter = panel.querySelector('[data-bet-filter]');
    if (filter) {
      filter.addEventListener('change', () => applyFilter(panel, filter.value));
    }
    // Line pickers (totals / spreads)
    panel.querySelectorAll('[data-line-pick]').forEach((sel) => {
      sel.addEventListener('change', () => {
        const mk = sel.dataset.linePick;
        const target = String(sel.value);
        panel.querySelectorAll('[data-line-box="' + mk + '"]').forEach((box) => {
          if (box.dataset.lineIdx === target) box.removeAttribute('hidden');
          else box.setAttribute('hidden', '');
        });
      });
    });
  });
})();

/* =====================================================================
   v2 ADDITIONS — theme toggle, favorites, mobile tab bar,
   pinned-on-home renderer, qual carousel auto-rotate, share helpers
   ===================================================================== */
(function(){
  'use strict';

  // ---------- Theme toggle ----------
  function applyTheme(t) {
    document.documentElement.setAttribute('data-bs-theme', t);
    document.documentElement.classList.toggle('dk-dark', t === 'dark');
  }
  var stored = (function(){ try { return localStorage.getItem('dk-theme'); } catch(e){ return null; } })();
  applyTheme(stored === 'dark' ? 'dark' : 'light');
  var themeBtn = document.getElementById('dkThemeBtn');
  if (themeBtn) {
    themeBtn.addEventListener('click', function(){
      var cur = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
      var next = cur === 'dark' ? 'light' : 'dark';
      try { localStorage.setItem('dk-theme', next); } catch(e){}
      applyTheme(next);
    });
  }

  // ---------- Favorites library (window.DK.fav) ----------
  var FAV_KEY = 'dk-favs-v1';
  var SEARCH_KEY = 'dk-searches-v1';
  function escHtml(s){ return String(s||'').replace(/[&<>"']/g, function(c){ return ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }
  function escAttr(s){ return escHtml(s); }
  function readAll() {
    try {
      var parsed = JSON.parse(localStorage.getItem(FAV_KEY)) || {};
      parsed.teams = parsed.teams || [];
      parsed.competitions = parsed.competitions || [];
      parsed.comparisons = parsed.comparisons || [];
      return parsed;
    }
    catch(e) { return { teams: [], competitions: [], comparisons: [] }; }
  }
  function writeAll(d) {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(d)); } catch(e){}
    document.dispatchEvent(new CustomEvent('dk:favs-changed', { detail: d }));
  }
  function has(kind, id) {
    var all = readAll();
    var arr = all[kind] || [];
    var key = String(id);
    return arr.some(function(it){ return String(it.id) === key; });
  }
  function toggle(kind, item) {
    var all = readAll();
    var arr = all[kind] || (all[kind] = []);
    var key = String(item.id);
    var idx = arr.findIndex(function(it){ return String(it.id) === key; });
    if (idx >= 0) { arr.splice(idx, 1); writeAll(all); return false; }
    arr.unshift(item);
    if (arr.length > 30) arr.length = 30;
    writeAll(all); return true;
  }
  function removeItem(kind, id) {
    var all = readAll();
    var arr = all[kind] || [];
    var key = String(id);
    all[kind] = arr.filter(function(it){ return String(it.id) !== key; });
    writeAll(all);
  }
  function readSearches() {
    try {
      var parsed = JSON.parse(localStorage.getItem(SEARCH_KEY)) || {};
      parsed.recent = parsed.recent || [];
      parsed.saved = parsed.saved || [];
      return parsed;
    } catch(e) {
      return { recent: [], saved: [] };
    }
  }
  function writeSearches(d) {
    try { localStorage.setItem(SEARCH_KEY, JSON.stringify(d)); } catch(e){}
    document.dispatchEvent(new CustomEvent('dk:searches-changed', { detail: d }));
  }
  function saveRecentSearch(query) {
    var q = String(query || '').trim();
    if (q.length < 2) return;
    var all = readSearches();
    all.recent = all.recent.filter(function(it){ return String(it.query).toLowerCase() !== q.toLowerCase(); });
    all.recent.unshift({ query: q, at: Date.now() });
    if (all.recent.length > 10) all.recent.length = 10;
    writeSearches(all);
  }
  function pinSearch(query) {
    var q = String(query || '').trim();
    if (q.length < 2) return;
    var all = readSearches();
    all.saved = all.saved.filter(function(it){ return String(it.query).toLowerCase() !== q.toLowerCase(); });
    all.saved.unshift({ query: q, at: Date.now() });
    if (all.saved.length > 12) all.saved.length = 12;
    writeSearches(all);
  }
  function removeSearch(kind, query) {
    var all = readSearches();
    all[kind] = (all[kind] || []).filter(function(it){ return String(it.query).toLowerCase() !== String(query || '').toLowerCase(); });
    writeSearches(all);
  }
  window.DK = window.DK || {};
  window.DK.fav = { readAll: readAll, has: has, toggle: toggle, remove: removeItem };
  window.DK.searches = { readAll: readSearches, saveRecent: saveRecentSearch, pin: pinSearch, remove: removeSearch };

  // ---------- Star buttons (data-fav-toggle) ----------
  function refreshStarStates(){
    document.querySelectorAll('[data-fav-toggle]').forEach(function(btn){
      var kind = btn.getAttribute('data-fav-kind');
      var id = btn.getAttribute('data-fav-id');
      btn.classList.toggle('is-on', has(kind, id));
      btn.setAttribute('aria-pressed', has(kind, id) ? 'true' : 'false');
    });
  }
  function refreshPinnedComparisonButtons() {
    document.querySelectorAll('[data-pin-comparison]').forEach(function(btn){
      var id = btn.getAttribute('data-id');
      var on = has('comparisons', id);
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.innerHTML = on
        ? '<i class="bi bi-pin-angle-fill me-1"></i>Pinned'
        : '<i class="bi bi-pin-angle me-1"></i>Pin comparison';
    });
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-fav-toggle]');
    if (!btn) return;
    e.preventDefault(); e.stopPropagation();
    var kind = btn.getAttribute('data-fav-kind');
    var item = {
      id: btn.getAttribute('data-fav-id'),
      name: btn.getAttribute('data-fav-name'),
      meta: btn.getAttribute('data-fav-meta') || '',
      img: btn.getAttribute('data-fav-img') || '',
      href: btn.getAttribute('data-fav-href') || ''
    };
    var on = toggle(kind, item);
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-pin-comparison]');
    if (!btn) return;
    e.preventDefault();
    var item = {
      id: btn.getAttribute('data-id'),
      name: btn.getAttribute('data-title') || 'Comparison',
      meta: btn.getAttribute('data-meta') || '',
      img: btn.getAttribute('data-left-img') || '',
      img2: btn.getAttribute('data-right-img') || '',
      href: btn.getAttribute('data-href') || '',
      compareKind: btn.getAttribute('data-kind') || 'teams'
    };
    var on = toggle('comparisons', item);
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.innerHTML = on
      ? '<i class="bi bi-pin-angle-fill me-1"></i>Pinned'
      : '<i class="bi bi-pin-angle me-1"></i>Pin comparison';
  });

  // ---------- Favorites menu + counts ----------
  function totalCount() {
    var d = readAll();
    return (d.teams||[]).length + (d.competitions||[]).length + (d.comparisons||[]).length;
  }
  function renderFavCounts(){
    var n = totalCount();
    document.querySelectorAll('[data-fav-count]').forEach(function(el){
      el.textContent = n > 0 ? n : '';
      if (el.classList.contains('d-none') && n > 0) el.classList.remove('d-none');
      if (n === 0 && el.classList.contains('dk-fav-count') && el.parentElement && el.parentElement.classList.contains('dk-favs-btn')) el.classList.add('d-none');
    });
  }
  function renderFavMenu(){
    var menu = document.getElementById('dkFavsMenu');
    if (!menu) return;
    var d = readAll();
    var items = [];
    if (d.competitions && d.competitions.length) {
      items.push('<li class="dk-fav-section-title">Leagues</li>');
      d.competitions.forEach(function(c){
        items.push(
          '<li><div class="dk-fav-item">'+
            (c.img ? '<img src="'+escAttr(c.img)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span style="width:24px;display:inline-block"></span>')+
            '<a class="text-decoration-none text-reset flex-grow-1" href="'+escAttr(c.href)+'">'+
              '<div class="dk-fav-item-name">'+escHtml(c.name)+'</div>'+
              (c.meta ? '<div class="dk-fav-item-meta">'+escHtml(c.meta)+'</div>' : '')+
            '</a>'+
            '<button type="button" class="dk-fav-item-x" data-fav-remove="competitions" data-fav-id="'+escAttr(c.id)+'" aria-label="Remove"><i class="bi bi-x-lg"></i></button>'+
          '</div></li>'
        );
      });
    }
    if (d.teams && d.teams.length) {
      items.push('<li class="dk-fav-section-title">Teams</li>');
      d.teams.forEach(function(t){
        items.push(
          '<li><div class="dk-fav-item">'+
            (t.img ? '<img src="'+escAttr(t.img)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span style="width:24px;display:inline-block"></span>')+
            '<a class="text-decoration-none text-reset flex-grow-1" href="'+escAttr(t.href)+'">'+
              '<div class="dk-fav-item-name">'+escHtml(t.name)+'</div>'+
              (t.meta ? '<div class="dk-fav-item-meta">'+escHtml(t.meta)+'</div>' : '')+
            '</a>'+
            '<button type="button" class="dk-fav-item-x" data-fav-remove="teams" data-fav-id="'+escAttr(t.id)+'" aria-label="Remove"><i class="bi bi-x-lg"></i></button>'+
          '</div></li>'
        );
      });
    }
    if (d.comparisons && d.comparisons.length) {
      items.push('<li class="dk-fav-section-title">Comparisons</li>');
      d.comparisons.forEach(function(c){
        items.push(
          '<li><div class="dk-fav-item">'+
            '<span class="dk-fav-compare-pair">'+
              (c.img ? '<img src="'+escAttr(c.img)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span class="dk-fav-compare-fallback"><i class="bi bi-arrow-left-right"></i></span>')+
              (c.img2 ? '<img src="'+escAttr(c.img2)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span class="dk-fav-compare-fallback"><i class="bi bi-stars"></i></span>')+
            '</span>'+
            '<a class="text-decoration-none text-reset flex-grow-1" href="'+escAttr(c.href)+'">'+
              '<div class="dk-fav-item-name">'+escHtml(c.name)+'</div>'+
              (c.meta ? '<div class="dk-fav-item-meta">'+escHtml(c.meta)+'</div>' : '')+
            '</a>'+
            '<button type="button" class="dk-fav-item-x" data-fav-remove="comparisons" data-fav-id="'+escAttr(c.id)+'" aria-label="Remove"><i class="bi bi-x-lg"></i></button>'+
          '</div></li>'
        );
      });
    }
    if (!items.length) {
      menu.innerHTML = '<li class="dk-favs-empty text-muted small px-3 py-2">No favorites yet. Pin a team, league, or comparison to reopen it quickly.</li>';
    } else {
      menu.innerHTML = items.join('');
    }
  }
  document.addEventListener('click', function(e){
    var x = e.target.closest('[data-fav-remove]');
    if (!x) return;
    e.preventDefault(); e.stopPropagation();
    removeItem(x.getAttribute('data-fav-remove'), x.getAttribute('data-fav-id'));
  });

  // Open favs from mobile tab bar -> trigger Bootstrap dropdown on the navbar button
  var tabbarFavBtn = document.querySelector('[data-tabbar-favs]');
  if (tabbarFavBtn) {
    tabbarFavBtn.addEventListener('click', function(){
      var btn = document.getElementById('dkFavsBtn');
      if (!btn) return;
      try {
        var dd = bootstrap.Dropdown.getOrCreateInstance(btn);
        dd.toggle();
      } catch(e) { btn.click(); }
    });
  }

  document.addEventListener('dk:favs-changed', function(){
    refreshStarStates(); refreshPinnedComparisonButtons(); renderFavCounts(); renderFavMenu(); renderPinnedHome(); renderSavedComparisons();
  });
  document.addEventListener('dk:searches-changed', function(){
    renderSearchHistory();
  });

  // ---------- Pinned-on-home rendering ----------
  function renderPinnedHome(){
    var section = document.getElementById('dkPinnedHome');
    if (!section) return;
    var grid = section.querySelector('[data-pinned-grid]');
    if (!grid) return;
    var d = readAll();
    var items = [].concat(
      (d.competitions||[]).map(function(c){ return { kind: 'competitions', id: c.id, name: c.name, meta: c.meta || 'League', img: c.img, href: c.href }; }),
      (d.teams||[]).map(function(t){ return { kind: 'teams', id: t.id, name: t.name, meta: t.meta || 'Team', img: t.img, href: t.href }; }),
      (d.comparisons||[]).map(function(c){ return { kind: 'comparisons', id: c.id, name: c.name, meta: c.meta || 'Comparison', img: c.img, img2: c.img2, href: c.href }; })
    );
    if (!items.length) { section.classList.remove('has-items'); grid.innerHTML = ''; return; }
    section.classList.add('has-items');
    grid.innerHTML = items.map(function(it){
      return ''+
        '<a class="dk-pinned-card" href="'+escAttr(it.href)+'">'+
          (it.kind === 'comparisons'
            ? '<span class="dk-fav-compare-pair">'+
                (it.img ? '<img src="'+escAttr(it.img)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span class="dk-fav-compare-fallback"><i class="bi bi-arrow-left-right"></i></span>')+
                (it.img2 ? '<img src="'+escAttr(it.img2)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span class="dk-fav-compare-fallback"><i class="bi bi-stars"></i></span>')+
              '</span>'
            : (it.img ? '<img src="'+escAttr(it.img)+'" alt="" onerror="this.style.display=\'none\'"/>' : '<span class="dk-pinned-card-icon"><i class="bi bi-shield-fill"></i></span>'))+
          '<span class="dk-pinned-card-body">'+
            '<span class="dk-pinned-card-name">'+escHtml(it.name)+'</span>'+
            '<span class="dk-pinned-card-kind">'+escHtml(it.meta)+'</span>'+
          '</span>'+
          '<button type="button" class="dk-pinned-card-x" data-fav-remove="'+escAttr(it.kind)+'" data-fav-id="'+escAttr(it.id)+'" aria-label="Unpin"><i class="bi bi-x-lg"></i></button>'+
        '</a>';
    }).join('');
  }

  function renderSavedComparisons() {
    document.querySelectorAll('[data-saved-comparisons]').forEach(function(node){
      var kind = node.getAttribute('data-kind') || 'teams';
      var items = (readAll().comparisons || []).filter(function(it){ return (it.compareKind || 'teams') === kind; });
      if (!items.length) {
        node.innerHTML = '<div class="dk-empty-state"><span class="dk-empty-state-icon"><i class="bi bi-pin-angle"></i></span><div><p class="dk-empty-state-title">No pinned comparisons yet</p><p class="dk-empty-state-copy">Pin a comparison above and it will stay here for quick offline access.</p></div></div>';
        return;
      }
      node.innerHTML = items.map(function(it){
        return '' +
          '<a class="dk-saved-card" href="' + escAttr(it.href) + '">' +
            '<button type="button" class="dk-saved-card-remove" data-fav-remove="comparisons" data-fav-id="' + escAttr(it.id) + '" aria-label="Remove"><i class="bi bi-x-lg"></i></button>' +
            '<div class="dk-saved-card-title">' + escHtml(it.name) + '</div>' +
            '<div class="dk-saved-card-meta">' + escHtml(it.meta || 'Comparison') + '</div>' +
            '<div class="dk-saved-card-row">' +
              '<span class="dk-saved-card-badge">' + (it.img ? '<img src="' + escAttr(it.img) + '" alt=""/>' : '<i class="bi bi-arrow-left-right"></i>') + '</span>' +
              '<i class="bi bi-lightning-charge-fill text-muted"></i>' +
              '<span class="dk-saved-card-badge">' + (it.img2 ? '<img src="' + escAttr(it.img2) + '" alt=""/>' : '<i class="bi bi-stars"></i>') + '</span>' +
            '</div>' +
          '</a>';
      }).join('');
    });
  }

  function renderSearchHistory() {
    var data = readSearches();
    document.querySelectorAll('[data-search-history-pinned]').forEach(function(node){
      var items = data.saved || [];
      if (!items.length) {
        node.innerHTML = '<div class="dk-empty-state"><span class="dk-empty-state-icon"><i class="bi bi-bookmarks"></i></span><div><p class="dk-empty-state-title">No saved searches yet</p><p class="dk-empty-state-copy">Save a query and it will stay ready to reopen instantly from this browser.</p></div></div>';
        return;
      }
      node.innerHTML = items.map(function(it){
        return '<a class="dk-search-history-chip" href="/search?q=' + encodeURIComponent(it.query) + '"><i class="bi bi-bookmark-fill"></i>' + escHtml(it.query) + '</a>';
      }).join('');
    });
    document.querySelectorAll('[data-search-history-recent]').forEach(function(node){
      var items = data.recent || [];
      if (!items.length) {
        node.innerHTML = '';
        return;
      }
      node.innerHTML = items.map(function(it){
        return '<a class="dk-search-history-chip" href="/search?q=' + encodeURIComponent(it.query) + '"><i class="bi bi-clock-history"></i>' + escHtml(it.query) + '</a>';
      }).join('');
    });
  }

  document.addEventListener('submit', function(e){
    var form = e.target.closest('[data-save-search-form]');
    if (!form) return;
    var input = form.querySelector('input[name="q"]');
    if (!input) return;
    saveRecentSearch(input.value);
  });
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-save-search-button]');
    if (!btn) return;
    e.preventDefault();
    pinSearch(btn.getAttribute('data-query') || '');
    btn.innerHTML = '<i class="bi bi-bookmark-check-fill me-1"></i>Saved';
  });

  refreshStarStates();
  refreshPinnedComparisonButtons();
  renderFavCounts();
  renderFavMenu();
  renderPinnedHome();
  renderSavedComparisons();
  renderSearchHistory();

  // ---------- Startup sync chooser ----------
  (function(){
    var dialog = document.querySelector('[data-startup-sync-dialog]');
    if (!dialog) return;

    var SESSION_KEY = 'dk-startup-sync-choice-v1';
    var startBtn = dialog.querySelector('[data-startup-sync-start]');
    var continueBtn = dialog.querySelector('[data-startup-sync-continue]');
    var titleEl = dialog.querySelector('[data-startup-sync-status-title]');
    var copyEl = dialog.querySelector('[data-startup-sync-status-copy]');
    var progressEl = dialog.querySelector('[data-startup-sync-bar]');
    var progressLabelEl = dialog.querySelector('[data-startup-sync-progress-label]');
    var errorsEl = dialog.querySelector('[data-startup-sync-errors]');
    var cachedEl = dialog.querySelector('[data-startup-cached-matches]');
    var leagueCountEl = dialog.querySelector('[data-startup-competition-count]');
    var modeEl = dialog.querySelector('[data-startup-mode-label]');
    var logEl = dialog.querySelector('[data-startup-sync-log]');
    var pollTimer = null;

    function readChoice() {
      try { return sessionStorage.getItem(SESSION_KEY) || ''; } catch (_) { return ''; }
    }

    function writeChoice(value) {
      try { sessionStorage.setItem(SESSION_KEY, value); } catch (_) {}
    }

    function show() {
      dialog.hidden = false;
      dialog.setAttribute('aria-hidden', 'false');
      document.body.classList.add('dk-startup-gate-open');
    }

    function hide() {
      dialog.hidden = true;
      dialog.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('dk-startup-gate-open');
    }

    function logIcon(level) {
      if (level === 'error') return 'bi bi-exclamation-octagon-fill';
      if (level === 'warn') return 'bi bi-exclamation-triangle-fill';
      return 'bi bi-check-circle-fill';
    }

    function renderLog(items) {
      if (!logEl) return;
      var rows = Array.isArray(items) ? items.slice(-4) : [];
      if (!rows.length) {
        logEl.hidden = true;
        logEl.innerHTML = '';
        return;
      }
      logEl.hidden = false;
      logEl.innerHTML = rows.map(function(item){
        var level = String((item && item.level) || 'info');
        var ts = (item && item.ts) ? ('<strong>' + escHtml(item.ts) + '</strong>') : '<strong>Now</strong>';
        return '' +
          '<div class="dk-startup-gate__log-item is-' + escAttr(level) + '">' +
            '<i class="' + escAttr(logIcon(level)) + '"></i>' +
            '<span>' + ts + ' ' + escHtml((item && item.msg) || '') + '</span>' +
          '</div>';
      }).join('');
    }

    function setProgress(pct) {
      if (!progressEl) return;
      var safePct = Math.max(0, Math.min(100, Number(pct) || 0));
      progressEl.style.width = safePct + '%';
    }

    function setSummary(snapshot) {
      if (!snapshot) return;
      if (cachedEl && snapshot.cached_matches != null) cachedEl.textContent = String(snapshot.cached_matches);
      if (leagueCountEl && snapshot.competition_count != null) leagueCountEl.textContent = String(snapshot.competition_count);
      if (modeEl) modeEl.textContent = snapshot.offline_mode ? 'Offline mode' : 'Online ready';
    }

    function applySnapshot(snapshot) {
      if (!snapshot) return;
      setSummary(snapshot);

      var total = Number(snapshot.total || snapshot.competition_count || 0);
      var progress = Number(snapshot.progress || 0);
      var running = !!snapshot.running;
      var lastRun = snapshot.last_run || '';
      var pct = total > 0 ? Math.round((progress / total) * 100) : (running ? 12 : 0);

      setProgress(pct);
      renderLog(snapshot.log || []);

      if (titleEl) {
        if (running) {
          titleEl.textContent = 'Syncing your offline archive';
        } else if (lastRun) {
          titleEl.textContent = 'Local archive ready';
        } else {
          titleEl.textContent = 'Choose your launch mode';
        }
      }

      if (copyEl) {
        if (running) {
          copyEl.textContent = snapshot.message || 'DKSPORTS is importing past matchweeks, fixtures, scores, standings, and scorers from the API.';
        } else if (lastRun) {
          copyEl.textContent = 'Your local football archive is already usable offline. You can browse now or run another refresh before you continue.';
        } else {
          copyEl.textContent = snapshot.message || 'Start with a live sync or continue directly from the data already cached on this machine.';
        }
      }

      if (progressLabelEl) {
        progressLabelEl.textContent = total > 0
          ? (progress + ' / ' + total + ' leagues')
          : 'Local archive';
      }

      if (errorsEl) {
        var errCount = Number(snapshot.errors || 0);
        errorsEl.textContent = errCount > 0
          ? (errCount + ' sync issue' + (errCount === 1 ? '' : 's') + ' detected so far.')
          : (running ? 'Sync is healthy so far.' : 'No sync issues detected yet.');
      }

      if (startBtn) {
        startBtn.disabled = running;
        startBtn.innerHTML = running
          ? '<i class="bi bi-arrow-repeat me-2"></i>Syncing now...'
          : '<i class="bi bi-cloud-arrow-down-fill me-2"></i>Sync API data now';
      }

      if (continueBtn) {
        continueBtn.textContent = running ? 'Continue while syncing' : 'Continue locally';
      }

      if (!running && pollTimer) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function pollStatus() {
      return fetch('/startup/sync/status', { credentials: 'same-origin' })
        .then(function(r){ return r.ok ? r.json() : Promise.reject(r); })
        .then(function(data){
          applySnapshot(data);
          return data;
        })
        .catch(function(){
          if (copyEl) copyEl.textContent = 'Startup sync status is temporarily unavailable. You can still continue locally.';
          if (errorsEl) errorsEl.textContent = 'Could not read sync status right now.';
          return null;
        });
    }

    function ensurePolling() {
      if (pollTimer) return;
      pollTimer = window.setInterval(function(){
        pollStatus().then(function(snapshot){
          if (!snapshot || !snapshot.running) {
            if (pollTimer) {
              window.clearInterval(pollTimer);
              pollTimer = null;
            }
          }
        });
      }, 3200);
    }

    function maybeShow() {
      if (!readChoice()) show();
      else hide();
    }

    if (startBtn) {
      startBtn.addEventListener('click', function(){
        writeChoice('sync');
        show();
        if (copyEl) copyEl.textContent = 'Starting sync for your tracked leagues...';
        if (errorsEl) errorsEl.textContent = 'Connecting to the providers now.';
        startBtn.disabled = true;
        fetch('/startup/sync/start', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }
        })
          .then(function(r){ return r.ok ? r.json() : Promise.reject(r); })
          .then(function(data){
            applySnapshot(data);
            ensurePolling();
          })
          .catch(function(){
            startBtn.disabled = false;
            if (titleEl) titleEl.textContent = 'Could not start sync';
            if (copyEl) copyEl.textContent = 'DKSPORTS could not start the API refresh right now. You can continue locally and retry from Settings.';
            if (errorsEl) errorsEl.textContent = 'The startup sync request failed.';
          });
      });
    }

    if (continueBtn) {
      continueBtn.addEventListener('click', function(){
        writeChoice('local');
        hide();
      });
    }

    maybeShow();
    pollStatus().then(function(snapshot){
      if (snapshot && snapshot.running) ensurePolling();
      if (!readChoice()) show();
    });
  })();

  // ---------- Global sync ribbon ----------
  (function(){
    var ribbon = document.querySelector('[data-sync-ribbon]');
    if (!ribbon) return;
    var titleEl = ribbon.querySelector('[data-sync-ribbon-title]');
    var metaEl = ribbon.querySelector('[data-sync-ribbon-meta]');
    var offline = ribbon.getAttribute('data-offline') === '1';
    if (offline) return;

    function applyStatus(status) {
      if (!status) return;
      if (status.offline_mode) {
        ribbon.classList.add('is-offline');
        if (titleEl) titleEl.textContent = 'Offline mode active';
        if (metaEl) metaEl.textContent = 'Serving cached football data without live provider calls.';
        return;
      }
      var running = !!status.running;
      ribbon.classList.toggle('is-offline', false);
      if (titleEl) titleEl.textContent = running ? 'Background sync running' : 'Sync monitor ready';
      if (metaEl) {
        if (running) {
          metaEl.textContent = status.current ? ('Refreshing ' + status.current + ' in the background.') : 'Refreshing cached football data in the background.';
        } else if (status.finished_at) {
          metaEl.textContent = 'Last sync finished at ' + status.finished_at + '. Cached data is ready for offline browsing.';
        } else {
          metaEl.textContent = 'Checking provider and cache freshness in the background.';
        }
      }
    }

    function poll() {
      fetch('/settings/sync-status', { credentials: 'same-origin' })
        .then(function(r){ return r.ok ? r.json() : Promise.reject(r); })
        .then(applyStatus)
        .catch(function(){
          if (metaEl) metaEl.textContent = 'Sync status is temporarily unavailable, but your cached catalog is still ready.';
        });
    }

    poll();
    window.setInterval(poll, 45000);
  })();

  // ---------- Qualification carousel auto-rotate ----------
  (function(){
    var stage = document.querySelector('[data-qual-stage]');
    if (!stage) return;
    var slides = Array.from(stage.querySelectorAll('[data-qual-slide]'));
    if (slides.length <= 1) return;
    var idx = 0;
    var timer = null;
    function show(i){
      slides.forEach(function(s, n){ s.classList.toggle('is-active', n === i); });
      var counter = document.querySelector('[data-qual-counter]');
      if (counter) counter.textContent = (i+1) + ' / ' + slides.length;
      idx = i;
    }
    function nextSlide(){ show((idx + 1) % slides.length); }
    function start(){ stop(); timer = setInterval(nextSlide, 6500); }
    function stop(){ if (timer) { clearInterval(timer); timer = null; } }
    start();
    stage.addEventListener('mouseenter', stop);
    stage.addEventListener('mouseleave', start);
  })();

  // ---------- Share button ----------
  var shareToast = null;
  function showToast(msg){
    if (!shareToast) {
      shareToast = document.createElement('div');
      shareToast.className = 'dk-share-toast';
      document.body.appendChild(shareToast);
    }
    shareToast.textContent = msg;
    shareToast.classList.add('show');
    setTimeout(function(){ shareToast.classList.remove('show'); }, 1800);
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest('[data-dk-share]');
    if (!btn) return;
    e.preventDefault();
    var url = btn.getAttribute('data-dk-share-url') || window.location.href;
    var title = btn.getAttribute('data-dk-share-title') || document.title;
    var text = btn.getAttribute('data-dk-share-text') || '';
    if (navigator.share) {
      navigator.share({ title: title, text: text, url: url }).catch(function(){});
    } else if (navigator.clipboard) {
      navigator.clipboard.writeText(url).then(function(){ showToast('Link copied!'); });
    } else {
      window.prompt('Copy share link:', url);
    }
  });
})();
