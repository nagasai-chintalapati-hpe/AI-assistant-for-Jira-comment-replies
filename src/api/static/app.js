/* Theme toggle */
var THEME_KEY = 'hpe-ui-theme';

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  var btn = document.getElementById('themeToggle');
  if (btn) btn.textContent = t === 'dark' ? 'Light' : 'Dark';
  localStorage.setItem(THEME_KEY, t);
}

function toggleTheme() {
  var current = document.documentElement.getAttribute('data-theme');
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

applyTheme(localStorage.getItem(THEME_KEY) || 'dark');

/* Bind theme toggle button */
var _themeBtn = document.getElementById('themeToggle');
if (_themeBtn) _themeBtn.addEventListener('click', toggleTheme);

/* Clear all drafts */
document.addEventListener('DOMContentLoaded', function () {
  var clearBtn = document.getElementById('clearAllBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      var n = clearBtn.getAttribute('data-total');
      if (confirm('Delete all ' + n + ' drafts? This cannot be undone.')) {
        document.getElementById('clearForm').submit();
      }
    });
  }

  /* Approve form: sync edited textarea */
  var approveForm = document.getElementById('approveForm');
  if (approveForm) {
    approveForm.addEventListener('submit', function () {
      var edited = document.getElementById('draftBody');
      var hidden = document.getElementById('approveBody');
      if (edited && hidden) {
        hidden.value = edited.value;
      }
    });
  }

  /* Copy draft button */
  var copyBtn = document.getElementById('copyBtn');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var text = document.getElementById('draftBody').value;
      var label = document.getElementById('copyLabel');

      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () {
          label.textContent = '\u2713 Copied!';
          setTimeout(function () { label.textContent = 'Copy'; }, 2000);
        }).catch(function () {
          fallbackCopy(text, label);
        });
      } else {
        fallbackCopy(text, label);
      }
    });
  }

  /* Apply data-width attributes */
  document.querySelectorAll('[data-width]').forEach(function (el) {
    el.style.width = el.getAttribute('data-width') + '%';
  });
});

function fallbackCopy(text, label) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand('copy');
    label.textContent = '\u2713 Copied!';
  } catch (_) {
    label.textContent = 'Failed';
  }
  document.body.removeChild(ta);
  setTimeout(function () { label.textContent = 'Copy'; }, 2000);
}
