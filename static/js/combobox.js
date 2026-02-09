/**
 * Combobox — Auto-enhancing searchable dropdown
 * Converts <select class="soft-input"> into searchable comboboxes.
 * Supports single-select, multi-select, Alpine.js x-model, and dark mode.
 *
 * Usage: Include this script. All <select> elements with class "soft-input"
 * (or data-combobox attribute) are auto-enhanced on DOMContentLoaded.
 * Add data-no-combobox to skip enhancement on specific selects.
 * Add data-combobox-placeholder="Search..." for custom placeholder text.
 */
(function () {
  'use strict';

  const DEBOUNCE_MS = 80;
  let uid = 0;

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function createCombobox(select) {
    if (select._comboboxInit) return;
    select._comboboxInit = true;

    const id = 'cbx-' + (++uid);
    const isMulti = select.multiple;
    const placeholder = select.dataset.comboboxPlaceholder ||
      select.querySelector('option[value=""]')?.textContent ||
      'Search...';

    // Collect options
    function getOptions() {
      const opts = [];
      for (const opt of select.options) {
        if (opt.value === '' && opt.index === 0 && !isMulti) continue; // skip placeholder option
        opts.push({
          value: opt.value,
          label: opt.textContent.trim(),
          disabled: opt.disabled,
          selected: opt.selected,
        });
      }
      return opts;
    }

    // Build wrapper
    const wrapper = document.createElement('div');
    wrapper.className = 'cbx-wrap';
    wrapper.id = id;
    wrapper.setAttribute('data-multi', isMulti ? '1' : '');

    // Transfer relevant classes (width classes etc.)
    const transferClasses = [];
    for (const cls of select.classList) {
      if (cls.startsWith('w-') || cls.startsWith('max-w-') || cls.startsWith('min-w-') ||
          cls === 'w-full' || cls.startsWith('flex') || cls.startsWith('col-span') ||
          cls.startsWith('sm:') || cls.startsWith('md:') || cls.startsWith('lg:')) {
        transferClasses.push(cls);
      }
    }
    if (transferClasses.length) wrapper.classList.add(...transferClasses);

    // Input field
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cbx-input';
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('autocorrect', 'off');
    input.setAttribute('autocapitalize', 'off');
    input.setAttribute('spellcheck', 'false');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', id + '-list');
    input.setAttribute('aria-autocomplete', 'list');
    input.placeholder = placeholder;
    if (select.disabled) input.disabled = true;
    if (select.required) input.required = true;

    // Display area (for showing current selection)
    const display = document.createElement('div');
    display.className = 'cbx-display';

    // Chevron
    const chevron = document.createElement('div');
    chevron.className = 'cbx-chevron';
    chevron.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 4.5L6 7.5L9 4.5"/></svg>';

    // Clear button (single select only)
    const clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'cbx-clear hidden';
    clearBtn.tabIndex = -1;
    clearBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 3l6 6M9 3l-6 6"/></svg>';
    clearBtn.title = 'Clear';

    // Dropdown list
    const dropdown = document.createElement('div');
    dropdown.className = 'cbx-dropdown hidden';
    dropdown.id = id + '-list';
    dropdown.setAttribute('role', 'listbox');
    if (isMulti) dropdown.setAttribute('aria-multiselectable', 'true');

    // Assemble
    const inputWrap = document.createElement('div');
    inputWrap.className = 'cbx-input-wrap';
    inputWrap.appendChild(display);
    inputWrap.appendChild(input);
    inputWrap.appendChild(clearBtn);
    inputWrap.appendChild(chevron);

    wrapper.appendChild(inputWrap);
    wrapper.appendChild(dropdown);

    // Hide original select but keep in DOM for form submission
    select.style.display = 'none';
    select.setAttribute('aria-hidden', 'true');
    select.tabIndex = -1;
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select); // move select inside wrapper

    // State
    let options = getOptions();
    let focused = -1;
    let isOpen = false;
    let query = '';

    // Render functions
    function getSelectedValues() {
      const vals = [];
      for (const opt of select.options) {
        if (opt.selected && opt.value !== '') vals.push(opt.value);
      }
      return vals;
    }

    function getSelectedLabels() {
      const labels = [];
      for (const opt of select.options) {
        if (opt.selected && opt.value !== '') labels.push(opt.textContent.trim());
      }
      return labels;
    }

    function updateDisplay() {
      const selected = getSelectedLabels();
      const values = getSelectedValues();

      if (isMulti) {
        if (selected.length === 0) {
          display.innerHTML = '<span class="cbx-placeholder">' + escapeHtml(placeholder) + '</span>';
          display.classList.remove('has-value');
        } else if (selected.length <= 2) {
          display.innerHTML = selected.map(function (l) {
            return '<span class="cbx-tag">' + escapeHtml(l) + '</span>';
          }).join('');
          display.classList.add('has-value');
        } else {
          display.innerHTML = '<span class="cbx-tag">' + escapeHtml(selected[0]) + '</span>' +
            '<span class="cbx-tag-count">+' + (selected.length - 1) + ' more</span>';
          display.classList.add('has-value');
        }
        clearBtn.classList.toggle('hidden', selected.length === 0);
      } else {
        if (selected.length === 0) {
          display.textContent = '';
          display.classList.remove('has-value');
          input.placeholder = placeholder;
          clearBtn.classList.add('hidden');
        } else {
          display.textContent = selected[0];
          display.classList.add('has-value');
          input.placeholder = '';
          clearBtn.classList.remove('hidden');
        }
      }

      // Sync input value when not open — keep selected label for required validation
      if (!isOpen && !isMulti) {
        var sel = getSelectedLabels();
        input.value = sel.length ? sel[0] : '';
        input.style.color = sel.length ? 'transparent' : '';
      }
    }

    function renderOptions(filter) {
      const q = (filter || '').toLowerCase();
      let html = '';
      let visibleCount = 0;
      const selectedVals = new Set(getSelectedValues());

      options.forEach(function (opt, i) {
        const matchLabel = opt.label.toLowerCase();
        const matchValue = opt.value.toLowerCase();
        if (q && matchLabel.indexOf(q) === -1 && matchValue.indexOf(q) === -1) return;

        const isSelected = selectedVals.has(opt.value);
        const isFocused = i === focused;
        visibleCount++;

        html += '<div class="cbx-option' +
          (isSelected ? ' selected' : '') +
          (isFocused ? ' focused' : '') +
          (opt.disabled ? ' disabled' : '') +
          '" data-index="' + i + '" data-value="' + escapeHtml(opt.value) + '" role="option"' +
          ' aria-selected="' + isSelected + '">';

        if (isMulti) {
          html += '<span class="cbx-check">' +
            (isSelected ? '<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2"><path d="M2.5 6l2.5 2.5 4.5-5"/></svg>' : '') +
            '</span>';
        }

        // Highlight matching text
        if (q) {
          const idx = matchLabel.indexOf(q);
          if (idx >= 0) {
            html += escapeHtml(opt.label.slice(0, idx)) +
              '<mark class="cbx-highlight">' + escapeHtml(opt.label.slice(idx, idx + q.length)) + '</mark>' +
              escapeHtml(opt.label.slice(idx + q.length));
          } else {
            html += escapeHtml(opt.label);
          }
        } else {
          html += escapeHtml(opt.label);
        }

        html += '</div>';
      });

      if (visibleCount === 0) {
        html = '<div class="cbx-empty">No results found</div>';
      }

      dropdown.innerHTML = html;
    }

    function open() {
      if (isOpen || select.disabled) return;
      isOpen = true;
      wrapper.classList.add('open');
      dropdown.classList.remove('hidden');
      input.setAttribute('aria-expanded', 'true');
      focused = -1;
      query = '';
      input.value = '';
      input.style.color = '';
      renderOptions('');

      if (!isMulti) {
        display.style.opacity = '0.4';
      }

      // Position dropdown
      positionDropdown();

      requestAnimationFrame(function () {
        input.focus();
      });
    }

    function close() {
      if (!isOpen) return;
      isOpen = false;
      wrapper.classList.remove('open');
      dropdown.classList.add('hidden');
      input.setAttribute('aria-expanded', 'false');
      focused = -1;
      query = '';

      if (!isMulti) {
        // Keep selected label in input so native `required` validation passes
        var sel = getSelectedLabels();
        input.value = sel.length ? sel[0] : '';
        // Hide input text visually — display overlay shows the label
        input.style.color = sel.length ? 'transparent' : '';
        display.style.opacity = '1';
      } else {
        input.value = '';
      }
    }

    function positionDropdown() {
      const rect = inputWrap.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;

      dropdown.classList.remove('cbx-dropdown-up');
      if (spaceBelow < 220 && spaceAbove > spaceBelow) {
        dropdown.classList.add('cbx-dropdown-up');
      }
    }

    function selectOption(index) {
      const opt = options[index];
      if (!opt || opt.disabled) return;

      if (isMulti) {
        // Toggle selection
        const nativeOpt = select.options[select.querySelector('option[value=""]') ? index + 1 : index];
        if (!nativeOpt) return;
        nativeOpt.selected = !nativeOpt.selected;
        renderOptions(query);
      } else {
        // Single select
        select.value = opt.value;
        close();
      }

      // Fire events for Alpine.js / vanilla JS
      select.dispatchEvent(new Event('change', { bubbles: true }));
      select.dispatchEvent(new Event('input', { bubbles: true }));
      updateDisplay();
    }

    function clearSelection() {
      if (isMulti) {
        for (const opt of select.options) opt.selected = false;
      } else {
        select.value = '';
        // Select the placeholder option if it exists
        const placeholderOpt = select.querySelector('option[value=""]');
        if (placeholderOpt) placeholderOpt.selected = true;
      }
      select.dispatchEvent(new Event('change', { bubbles: true }));
      select.dispatchEvent(new Event('input', { bubbles: true }));
      updateDisplay();
      if (isOpen) renderOptions(query);
    }

    // Event handlers
    let debounceTimer;
    input.addEventListener('input', function () {
      query = input.value;
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () {
        focused = -1;
        renderOptions(query);
      }, DEBOUNCE_MS);
      if (!isOpen) open();
    });

    input.addEventListener('focus', function () {
      if (!isOpen) open();
    });

    input.addEventListener('keydown', function (e) {
      const visibleOpts = dropdown.querySelectorAll('.cbx-option:not(.disabled)');

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          if (!isOpen) { open(); return; }
          focused = Math.min(focused + 1, visibleOpts.length - 1);
          updateFocus(visibleOpts);
          break;

        case 'ArrowUp':
          e.preventDefault();
          if (!isOpen) { open(); return; }
          focused = Math.max(focused - 1, 0);
          updateFocus(visibleOpts);
          break;

        case 'Enter':
          e.preventDefault();
          if (isOpen && focused >= 0 && visibleOpts[focused]) {
            const idx = parseInt(visibleOpts[focused].dataset.index);
            selectOption(idx);
          } else if (!isOpen) {
            open();
          }
          break;

        case 'Escape':
          e.preventDefault();
          close();
          break;

        case 'Tab':
          close();
          break;

        case 'Backspace':
          if (isMulti && input.value === '') {
            // Remove last selected tag
            const selected = getSelectedValues();
            if (selected.length > 0) {
              const lastVal = selected[selected.length - 1];
              for (const opt of select.options) {
                if (opt.value === lastVal) {
                  opt.selected = false;
                  break;
                }
              }
              select.dispatchEvent(new Event('change', { bubbles: true }));
              updateDisplay();
              renderOptions(query);
            }
          }
          break;
      }
    });

    function updateFocus(visibleOpts) {
      dropdown.querySelectorAll('.cbx-option').forEach(function (el) {
        el.classList.remove('focused');
      });
      if (visibleOpts[focused]) {
        visibleOpts[focused].classList.add('focused');
        visibleOpts[focused].scrollIntoView({ block: 'nearest' });
      }
    }

    // Click on option
    dropdown.addEventListener('mousedown', function (e) {
      e.preventDefault(); // Prevent input blur
      const optEl = e.target.closest('.cbx-option');
      if (optEl && !optEl.classList.contains('disabled')) {
        const idx = parseInt(optEl.dataset.index);
        focused = idx;
        selectOption(idx);
      }
    });

    // Click on input wrapper opens dropdown
    inputWrap.addEventListener('mousedown', function (e) {
      if (e.target === clearBtn || clearBtn.contains(e.target)) return;
      e.preventDefault();
      if (isOpen) {
        close();
      } else {
        open();
      }
    });

    // Clear button
    clearBtn.addEventListener('mousedown', function (e) {
      e.preventDefault();
      e.stopPropagation();
      clearSelection();
      if (!isMulti) close();
    });

    // Close on outside click
    document.addEventListener('mousedown', function (e) {
      if (!wrapper.contains(e.target)) {
        close();
      }
    });

    // Watch for external changes to the select (e.g., Alpine.js programmatic changes)
    const observer = new MutationObserver(function () {
      options = getOptions();
      updateDisplay();
      if (isOpen) renderOptions(query);
    });
    observer.observe(select, { childList: true, subtree: true, attributes: true, attributeFilter: ['selected'] });

    // Also listen for change events dispatched externally
    select.addEventListener('change', function (e) {
      if (!e.isTrusted && !e._fromCombobox) {
        updateDisplay();
        if (isOpen) renderOptions(query);
      }
    });

    // Initial render
    updateDisplay();
  }

  // Auto-enhance on DOMContentLoaded
  function init() {
    var selects = document.querySelectorAll('select.soft-input, select[data-combobox]');
    selects.forEach(function (el) {
      // Skip hidden selects (already part of custom UI), single-option selects, or opted-out
      if (el.dataset.noCombobox !== undefined) return;
      if (el.style.display === 'none' || el.closest('.hidden') || el.classList.contains('hidden')) return;
      if (el.options.length <= 1) return;
      // Skip very small selects (< 3 options excluding placeholder) — they don't benefit from search
      var realOpts = 0;
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].value !== '') realOpts++;
      }
      if (realOpts < 3) return;

      createCombobox(el);
    });
  }

  // Run on DOMContentLoaded or immediately if already loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    // Small delay to let Alpine.js initialize first
    setTimeout(init, 50);
  }

  // Expose for manual usage
  window.Combobox = { enhance: createCombobox, init: init };
})();
