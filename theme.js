(function () {
  'use strict';

  const storageKey = 'piichan-appearance';
  const root = document.documentElement;

  function readTheme() {
    try {
      return localStorage.getItem(storageKey) === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  }

  function updateThemeColor(theme) {
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      meta = document.createElement('meta');
      meta.name = 'theme-color';
      document.head.appendChild(meta);
    }
    meta.content = theme === 'dark' ? '#050609' : '#eef1f6';
  }

  function renderButtons(theme) {
    document.querySelectorAll('.theme-toggle').forEach((button) => {
      const nextTheme = theme === 'dark' ? 'light' : 'dark';
      const icon = button.querySelector('.theme-toggle__icon');
      const label = button.querySelector('.theme-toggle__label');
      button.setAttribute('aria-label', `${nextTheme === 'light' ? 'ライト' : 'ダーク'}テーマに切り替える`);
      button.setAttribute('aria-pressed', String(theme === 'light'));
      button.title = `${nextTheme === 'light' ? 'Light' : 'Dark'} appearance`;
      if (icon) icon.textContent = theme === 'dark' ? '☾' : '☀';
      if (label) label.textContent = theme === 'dark' ? 'Dark' : 'Light';
    });
  }

  function applyTheme(theme, persist) {
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    updateThemeColor(theme);
    renderButtons(theme);
    if (persist) {
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // Appearance still applies when storage is unavailable.
      }
    }
  }

  function createToggle() {
    const button = document.createElement('button');
    button.className = 'theme-toggle';
    button.type = 'button';
    button.innerHTML = '<span class="theme-toggle__icon" aria-hidden="true"></span><span class="theme-toggle__label"></span>';
    button.addEventListener('click', () => {
      applyTheme(root.dataset.theme === 'light' ? 'dark' : 'light', true);
    });
    return button;
  }

  function installToggle() {
    const headers = document.querySelectorAll('.top-bar, .gate-topbar');
    if (headers.length) {
      headers.forEach((header) => {
        if (header.querySelector('.theme-toggle')) return;
        const toggle = createToggle();
        const homeLink = header.querySelector('.home-link, .back-link');
        header.insertBefore(toggle, homeLink || null);
      });
    } else if (!document.querySelector('.theme-toggle')) {
      const toggle = createToggle();
      toggle.style.position = 'fixed';
      toggle.style.zIndex = '100';
      toggle.style.top = '16px';
      toggle.style.right = '16px';
      document.body.appendChild(toggle);
    }
    renderButtons(root.dataset.theme);
  }

  function installGlassLighting() {
    if (!window.matchMedia('(pointer: fine)').matches) return;
    const selector = '.museum-stage, .gate-stage, .tool-card, .glass-card, .panel-card, .info-card, .support-group, .command-deck, .notice, .caution, .stream-result-card';
    document.querySelectorAll(selector).forEach((surface) => {
      surface.addEventListener('pointermove', (event) => {
        const bounds = surface.getBoundingClientRect();
        surface.style.setProperty('--glass-x', `${event.clientX - bounds.left}px`);
        surface.style.setProperty('--glass-y', `${event.clientY - bounds.top}px`);
      });
    });
  }

  applyTheme(readTheme(), false);

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      installToggle();
      installGlassLighting();
    }, { once: true });
  } else {
    installToggle();
    installGlassLighting();
  }
})();
