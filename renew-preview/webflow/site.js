// Designer-only hide classes (is-hidden-*) keep inactive panels out of the
// Webflow canvas. Webflow strips the hidden attribute on publish, so turn the
// class back into hidden here.
(function () {
  document.querySelectorAll('[class*="is-hidden-"]').forEach(function (el) {
    Array.prototype.slice.call(el.classList).forEach(function (c) {
      if (c.indexOf('is-hidden-') === 0) { el.classList.remove(c); el.hidden = true; }
    });
  });
})();

// Webflow build: inputs are only allowed inside forms, so the before/after
// range inputs ship as <div data-range> placeholders. Rebuild them first.
(function () {
  document.querySelectorAll('div[data-range]').forEach(function (d) {
    var i = document.createElement('input');
    i.type = 'range';
    i.className = d.className;
    if (d.dataset.id) i.id = d.dataset.id;
    i.min = d.dataset.min || '0';
    i.max = d.dataset.max || '100';
    i.value = d.dataset.value || '50';
    if (d.dataset.label) i.setAttribute('aria-label', d.dataset.label);
    d.parentNode.replaceChild(i, d);
  });
})();

// Renew Staging & Design — prototype interactions for the design pages.

(function () {
  // mobile nav
  var toggle = document.querySelector('.nav-toggle');
  var nav = document.querySelector('.site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      toggle.firstChild.textContent = open ? 'Close' : 'Menu';
    });
  }

  // before / after sliders
  document.querySelectorAll('.ba').forEach(function (ba) {
    var range = ba.querySelector('.ba__range');
    if (!range) return;
    var set = function () { ba.style.setProperty('--pos', range.value + '%'); };
    range.addEventListener('input', set);
    set();
  });

  // intent router (home) — rows act as tabs for the panel beside them
  document.querySelectorAll('[data-router]').forEach(function (router) {
    var tabs = router.querySelectorAll('[role="tab"]');
    var panels = router.querySelectorAll('[role="tabpanel"]');
    var select = function (tab) {
      tabs.forEach(function (t) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
      });
      panels.forEach(function (p) { p.hidden = p.id !== tab.getAttribute('aria-controls'); });
    };
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () { select(tab); });
      tab.addEventListener('keydown', function (e) {
        if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
        e.preventDefault();
        var next = tabs[(i + (e.key === 'ArrowDown' ? 1 : tabs.length - 1)) % tabs.length];
        select(next);
        next.focus();
      });
    });
  });

  // chips: filters and intake answers
  document.querySelectorAll('.filters').forEach(function (group) {
    var single = group.hasAttribute('data-single');
    group.querySelectorAll('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        if (single) {
          group.querySelectorAll('.chip').forEach(function (c) { c.setAttribute('aria-pressed', c === chip ? 'true' : 'false'); });
        } else {
          chip.setAttribute('aria-pressed', chip.getAttribute('aria-pressed') === 'true' ? 'false' : 'true');
        }
      });
    });
  });
})();

// Nov 12 call: team bios open in a shadow box instead of a page.
(function () {
  var cards = document.querySelectorAll('.card3--modal');
  if (!cards.length) return;
  var modal = document.createElement('div');
  modal.className = 'modal';
  modal.setAttribute('role', 'dialog');
  modal.setAttribute('aria-modal', 'true');
  modal.innerHTML = '<div class="modal__box">' +
    '<button class="btn btn--sm btn--plain modal__close" type="button" aria-label="Close">Close</button>' +
    '<div class="modal__photo"></div>' +
    '<div class="modal__body"><div class="modal__meta"><h3 class="h3 modal__name"></h3><span class="muted small modal__role"></span></div><div class="prose modal__bio"></div><a class="modal__email" href="#"></a></div>' +
    '</div>';
  document.body.appendChild(modal);
  var photo = modal.querySelector('.modal__photo');
  var name = modal.querySelector('.modal__name');
  var role = modal.querySelector('.modal__role');
  var bio = modal.querySelector('.modal__bio');
  var email = modal.querySelector('.modal__email');
  var last = null;
  var close = function () { modal.classList.remove('is-open'); if (last) last.focus(); };
  var open = function (card) {
    last = card;
    photo.innerHTML = card.querySelector('.slot').outerHTML;
    name.textContent = card.getAttribute('data-name') || '';
    role.textContent = card.getAttribute('data-role') || '';
    bio.innerHTML = (card.getAttribute('data-bio') || '').split('\n').map(function (p) { return '<p class="muted">' + p + '</p>'; }).join('');
    var em = card.getAttribute('data-email');
    email.hidden = !em;
    if (em) { email.textContent = em; email.href = 'mailto:' + em; }
    modal.classList.add('is-open');
    modal.querySelector('.modal__close').focus();
  };
  cards.forEach(function (card) { card.addEventListener('click', function () { open(card); }); });
  modal.querySelector('.modal__close').addEventListener('click', close);
  modal.addEventListener('click', function (e) { if (e.target === modal) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && modal.classList.contains('is-open')) close(); });
})();

// Webflow build: chip groups on the intake form write their selection into a
// hidden text field (data-field names it) so the answer reaches the form inbox.
(function () {
  document.querySelectorAll('.filters[data-field]').forEach(function (group) {
    var form = group.closest('form');
    var input = form && form.querySelector('input[name="' + group.getAttribute('data-field') + '"]');
    if (!input) return;
    var sync = function () {
      input.value = Array.prototype.slice.call(group.querySelectorAll('.chip[aria-pressed="true"]'))
        .map(function (c) { return c.textContent.trim(); }).join(', ');
    };
    group.addEventListener('click', function () { setTimeout(sync, 0); });
    sync();
  });
})();
