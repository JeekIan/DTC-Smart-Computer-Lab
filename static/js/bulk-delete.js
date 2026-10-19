/**
 * Shared "Delete Selected" behavior for list dashboards (Users, Class
 * Rosters, Labs, Inventory, Notifications, ...).
 *
 * Markup contract for any list that wants this feature:
 *
 *   <div class="js-bulk-delete" data-item-label="user">
 *     <form method="post" action="{% url 'users_bulk_delete' %}" class="js-bulk-delete-form">
 *       {% csrf_token %}
 *       <div class="bulk-toolbar js-bulk-toolbar">
 *         <span><span class="js-bulk-count">0</span> selected</span>
 *         <button type="button" class="bulk-delete-btn js-bulk-delete-btn">Delete Selected</button>
 *       </div>
 *       <table>
 *         <thead><tr><th><input type="checkbox" class="js-select-all"></th>...</tr></thead>
 *         <tbody>
 *           {% for row in rows %}
 *           <tr><td><input type="checkbox" class="js-row-check" value="{{ row.pk }}"></td>...</tr>
 *           {% endfor %}
 *         </tbody>
 *       </table>
 *     </form>
 *   </div>
 *
 * - ".js-select-all" and every ".js-row-check" must live inside the same
 *   ".js-bulk-delete" wrapper as the form (the checkboxes themselves don't
 *   need to be physically inside the <form> tag — this script re-parents
 *   the checked values into hidden inputs before submitting).
 * - "data-item-label" (optional) customizes the confirmation text, e.g.
 *   "user" -> "Delete 3 users?".
 * - "data-select-reveal" (optional, no value) keeps the row checkboxes
 *   hidden until "Select all" is ticked; they hide again once nothing is
 *   selected.
 * - "data-confirm-text" (optional) replaces the dialog's body text, for
 *   lists where "Delete" doesn't mean a permanent delete.
 */
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.js-bulk-delete').forEach(initBulkDelete);
  });

  function initBulkDelete(root) {
    var form = root.querySelector('.js-bulk-delete-form');
    var selectAll = root.querySelector('.js-select-all');
    var toolbar = root.querySelector('.js-bulk-toolbar');
    var countEl = toolbar ? toolbar.querySelector('.js-bulk-count') : null;
    var deleteBtn = toolbar ? toolbar.querySelector('.js-bulk-delete-btn') : null;
    var itemLabel = root.dataset.itemLabel || 'record';
    // data-select-reveal: row checkboxes stay hidden until "Select all" is
    // ticked (see the .sel-active rule in base.html).
    var reveal = root.hasAttribute('data-select-reveal');
    if (!form || !deleteBtn) return;

    function rowChecks() {
      return root.querySelectorAll('.js-row-check');
    }

    function refresh() {
      var checked = root.querySelectorAll('.js-row-check:checked');
      if (reveal && checked.length === 0) root.classList.remove('sel-active');
      if (toolbar) toolbar.classList.toggle('is-visible', checked.length > 0);
      if (countEl) countEl.textContent = checked.length;
      if (selectAll) {
        var all = rowChecks();
        selectAll.checked = all.length > 0 && checked.length === all.length;
        selectAll.indeterminate = checked.length > 0 && checked.length < all.length;
      }
    }

    root.addEventListener('change', function (e) {
      if (e.target.classList.contains('js-row-check')) refresh();
      if (selectAll && e.target === selectAll) {
        rowChecks().forEach(function (cb) { cb.checked = selectAll.checked; });
        if (reveal) root.classList.toggle('sel-active', selectAll.checked);
        refresh();
      }
    });

    function doSubmit(ids) {
      form.querySelectorAll('input[name="selected_ids"]').forEach(function (i) { i.remove(); });
      ids.forEach(function (id) {
        var input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'selected_ids';
        input.value = id;
        form.appendChild(input);
      });
      form.submit();
    }

    deleteBtn.addEventListener('click', function () {
      var checked = root.querySelectorAll('.js-row-check:checked');
      if (!checked.length) return;
      var ids = Array.prototype.map.call(checked, function (cb) { return cb.value; });
      var count = ids.length;
      var plural = count === 1 ? '' : 's';
      var text = root.dataset.confirmText || ('This will permanently delete ' + count + ' ' + itemLabel + plural + '. This cannot be undone.');

      // Reuse the app-wide SweetAlert2 helper (see base.html) when it's
      // available, same confirm dialog every destructive action already
      // uses; fall back to a plain confirm() if Swal never loaded.
      if (typeof compulabConfirmSubmit === 'function') {
        // compulabConfirmSubmit submits the given form on confirm — build
        // the hidden inputs first, then hand it a form whose submit it
        // controls.
        form.querySelectorAll('input[name="selected_ids"]').forEach(function (i) { i.remove(); });
        ids.forEach(function (id) {
          var input = document.createElement('input');
          input.type = 'hidden';
          input.name = 'selected_ids';
          input.value = id;
          form.appendChild(input);
        });
        compulabConfirmSubmit(form, {
          title: 'Delete ' + count + ' selected ' + itemLabel + plural + '?',
          text: text,
          confirmButtonText: 'Yes, delete',
          confirmButtonColor: '#ff5d5d',
        });
      } else if (window.confirm('Delete ' + count + ' selected ' + itemLabel + plural + '? ' + text)) {
        doSubmit(ids);
      }
    });

    refresh();
  }
})();
