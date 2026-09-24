;;; lsp-bridge-source.el --- Local external source providers -*- lexical-binding: t; -*-
(require 'cl-lib)
(require 'json)
(require 'browse-url)
(require 'url-util)
(require 'xml)

(defvar lsp-bridge-jump-to-def-in-other-window)
(defvar lsp-bridge-enable-predicates)
(defvar lsp-bridge-peek-symbol-at-point)
(defvar lsp-bridge-python-command)
(declare-function lsp-bridge--position "lsp-bridge")
(declare-function lsp-bridge-find-def-fallback "lsp-bridge" (position))
(declare-function lsp-bridge-define--jump "lsp-bridge" (filename filehost position))
(declare-function lsp-bridge-peek-references--return "lsp-bridge-peek" (content count))
(declare-function lsp-bridge-call-file-api "lsp-bridge" (method &rest args))

(defgroup lsp-bridge-source nil "External source navigation." :group 'lsp-bridge)
(defcustom lsp-bridge-source-enable nil
  "Enable local source providers after definition lookup fails."
  :type 'boolean :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-python-command nil
  "Local Python 3.9+ for helpers, or nil to reuse the bridge interpreter.
For uv/pipx/launcher wrappers, nil uses an installed python3/python directly
so ordinary navigation cannot install an interpreter or dependencies."
  :type '(choice (const nil) file) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-cache-directory nil
  "Root of sources acquired by lsp-bridge, or nil for the OS user cache."
  :type '(choice (const nil) directory) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-haskell-ghc nil
  "Project GHC executable; nil uses PATH. May be buffer-local."
  :type '(choice (const nil) file) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-haskell-ghc-pkg nil
  "Project ghc-pkg executable; nil uses PATH. May be buffer-local."
  :type '(choice (const nil) file) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-haskell-packages nil
  "Exact package unit IDs for explicit environments or package selection."
  :type '(repeat string) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-haskell-package-dbs nil
  "Additional project package databases passed to ghc-pkg."
  :type '(repeat directory) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-haskell-roots nil
  "Registered sources: alists with compiler, unit and path string keys.
Example: ((\"compiler\" . \"ghc-9.12.2\") (\"unit\" . \"base-4.21.0.0-958c\")
          (\"path\" . \"/path/to/base\")). Each entry must assert an exact identity."
  :type '(repeat alist) :group 'lsp-bridge-source)
(defcustom lsp-bridge-source-max-tasks 2
  "Maximum number of isolated source helpers."
  :type 'integer :group 'lsp-bridge-source)
(defvar lsp-bridge-source--tasks nil)
(defvar lsp-bridge-source--sequence 0)
(defvar-local lsp-bridge-source--context nil)
(defvar-local lsp-bridge-source--documentation nil)
(defvar-local lsp-bridge-source--reading nil)
(defconst lsp-bridge-source--worker
  (expand-file-name "core/source/worker.py" (file-name-directory (or load-file-name buffer-file-name))))

(defun lsp-bridge-source--language ()
  (cond ((derived-mode-p 'haskell-mode 'haskell-ts-mode) "haskell")
        ((derived-mode-p 'java-mode 'java-ts-mode) "java")
        ((derived-mode-p 'rust-mode 'rust-ts-mode) "rust")
        ((derived-mode-p 'go-mode 'go-ts-mode) "go")
        ((derived-mode-p 'python-mode 'python-ts-mode) "python")
        ((derived-mode-p 'typescript-mode 'typescript-ts-mode 'tsx-ts-mode) "typescript")
        ((derived-mode-p 'js-mode 'js-ts-mode 'js2-mode) "javascript")
        (t "")))

(defun lsp-bridge-source--config ()
  `((cache . ,lsp-bridge-source-cache-directory)
    (ghc . ,lsp-bridge-source-haskell-ghc)
    (ghc_pkg . ,lsp-bridge-source-haskell-ghc-pkg)
    (packages . ,(vconcat lsp-bridge-source-haskell-packages))
    (package_dbs . ,(vconcat lsp-bridge-source-haskell-package-dbs))
    (roots . ,(vconcat lsp-bridge-source-haskell-roots))))

(defun lsp-bridge-source--capture (mode position)
  (when (and lsp-bridge-source-enable buffer-file-name
             (not (file-remote-p buffer-file-name))
             (equal (lsp-bridge-source--language) "haskell"))
    (save-restriction
      (widen)
      (let ((id (number-to-string (cl-incf lsp-bridge-source--sequence))))
        (setq lsp-bridge-source--context
              (list :id id :buffer (current-buffer) :window (selected-window)
                    :point (point) :tick (buffer-chars-modified-tick)
                    :other lsp-bridge-jump-to-def-in-other-window
                    :request `((request_id . ,id) (file . ,buffer-file-name)
                               (version . ,(buffer-chars-modified-tick))
                               (point . ,(1- (point))) (text . ,(buffer-substring-no-properties (point-min) (point-max)))
                               (position . ,position) (project . ,default-directory)
                               (server . ,(format "%s" (bound-and-true-p acm-backend-lsp-server-names)))
                               (mode . ,mode) (language . "haskell")
                               (config . ,(lsp-bridge-source--config)))))
        id))))

(defun lsp-bridge-source--valid-p (context)
  (let ((buffer (plist-get context :buffer)) (window (plist-get context :window)))
    (and context (buffer-live-p buffer) (window-live-p window)
         (eq window (selected-window))
         (eq (window-buffer window) buffer)
         (with-current-buffer buffer
           (and (eq context lsp-bridge-source--context)
                (= (point) (plist-get context :point))
                (= (buffer-chars-modified-tick) (plist-get context :tick)))))))

(defun lsp-bridge-source--invalidate ()
  (when (and lsp-bridge-source--context
             (not (lsp-bridge-source--valid-p lsp-bridge-source--context)))
    (setq lsp-bridge-source--context nil)))
(add-hook 'post-command-hook #'lsp-bridge-source--invalidate)
(defun lsp-bridge-source--changed (&rest _)
  (setq lsp-bridge-source--context nil))
(add-hook 'after-change-functions #'lsp-bridge-source--changed)

(defun lsp-bridge-source--request (method position)
  (let ((id (lsp-bridge-source--capture
             (if (equal method "peek_find_definition") "peek" "jump") position)))
    (if id
        (lsp-bridge-call-file-api method position id)
      (lsp-bridge-call-file-api method position))))

(defun lsp-bridge-source--fallback (file id position)
  "Route an unsuccessful definition response to its original live buffer."
  (when-let* ((buffer (find-buffer-visiting file)))
    (with-current-buffer buffer
      (let ((context lsp-bridge-source--context))
        (when (and (equal id (plist-get context :id)) (lsp-bridge-source--valid-p context))
          (lsp-bridge-source--enqueue "resolve" context
                                      (lambda (result) (lsp-bridge-source--resolved context position result))))))))

(defun lsp-bridge-source--browse (url)
  ;; macOS Launch Services drops fragments on file URLs; use a redirect page.
  (if (and (eq system-type 'darwin) (string-match-p "#" url))
      (let ((page (make-temp-file "lsp-bridge-source-" nil ".html")))
        (with-temp-file page
          (insert "<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"refresh\" content=\"0;url="
                  (xml-escape-string url) "\">"))
        (browse-url (concat "file://" (url-encode-url page))))
    (browse-url url)))

(defun lsp-bridge-source--resolved (context position response)
  (when (lsp-bridge-source--valid-p context)
    (with-current-buffer (plist-get context :buffer)
      (let* ((result (alist-get 'result response))
             (path (alist-get 'path result))
             (url (alist-get 'documentation result))
             (peek (equal (alist-get 'mode (plist-get context :request)) "peek")))
        (when (and url (not (equal url "")))
          (setq lsp-bridge-source--documentation url))
        (cond
         ((and url (not (equal url ""))
               (equal (alist-get 'mode (plist-get context :request)) "documentation"))
          (lsp-bridge-source--browse url))
         ((and path (not (equal path "")) (file-readable-p path))
          ;; Prepare read-only buffer before global mode hooks can start a server.
          (unless (find-buffer-visiting path)
            (let ((lsp-bridge-enable-predicates (list (lambda () nil))))
              (with-current-buffer (find-file-noselect path)
                (setq-local lsp-bridge-source--reading t)
                (setq-local buffer-read-only t))))
          (let ((target (list :line (alist-get 'line result) :character (alist-get 'character result))))
            (if peek
                (progn
                  (push path (nth 1 lsp-bridge-peek-symbol-at-point))
                  (push target (nth 2 lsp-bridge-peek-symbol-at-point))
                  (push 0 (nth 6 lsp-bridge-peek-symbol-at-point))
                  (lsp-bridge-peek-references--return nil 0))
              (setq-local lsp-bridge-jump-to-def-in-other-window (plist-get context :other))
              (lsp-bridge-define--jump path "" target))))
         ((and url (not (equal url "")))
          (if peek
              (message "Local source unavailable; use lsp-bridge-source-open-documentation. %s" (alist-get 'message result))
            (lsp-bridge-source--browse url)
            (message "%s" (alist-get 'message result))))
         (t
          (when-let* ((error (alist-get 'error response))) (message "Source provider: %s" error))
          (lsp-bridge-find-def-fallback position)))))))

(defun lsp-bridge-source--enqueue (action context callback)
  (let* ((request (plist-get context :request))
         (key (if (equal action "install")
                  (list action (alist-get 'project request) (alist-get 'config request))
                (list action (alist-get 'request_id request))))
         (existing (cl-find key lsp-bridge-source--tasks :key (lambda (task) (plist-get task :key)) :test #'equal)))
    (if existing
        (message "Source task already queued or running")
      (let* ((directory (make-temp-file "lsp-bridge-source-task-" t))
             (task (list :key key :action action :context context :callback callback
                         :directory directory :cancel (expand-file-name "cancel" directory)
                         :process nil :state 'queued)))
        (setq lsp-bridge-source--tasks (append lsp-bridge-source--tasks (list task)))
        (lsp-bridge-source--pump)))))

(defun lsp-bridge-source--python ()
  (or lsp-bridge-source-python-command
      (if (member (file-name-base lsp-bridge-python-command) '("uv" "pipx" "python-lsp-bridge"))
          (or (executable-find "python3") (executable-find "python")
              (error "Configure lsp-bridge-source-python-command to a local Python 3.9+"))
        lsp-bridge-python-command)))

(defun lsp-bridge-source--pump ()
  (let ((running (cl-count 'running lsp-bridge-source--tasks :key (lambda (task) (plist-get task :state)))))
    (dolist (task lsp-bridge-source--tasks)
      (when (and (< running (max 1 lsp-bridge-source-max-tasks)) (eq (plist-get task :state) 'queued))
        (cl-incf running)
        (setf (plist-get task :state) 'running)
        (let ((output (generate-new-buffer " *lsp-source-result*")) process)
          (condition-case err
              (progn
                (setq process (make-process
                               :name "lsp-bridge-source" :buffer output :connection-type 'pipe
                               :coding 'utf-8-unix :noquery t
                               :stderr (get-buffer-create "*lsp-bridge-source-log*")
                               :command (list (lsp-bridge-source--python) lsp-bridge-source--worker)
                               :sentinel (lambda (proc _event)
                                           (when (memq (process-status proc) '(exit signal))
                                             (lsp-bridge-source--finished task proc)))))
                (setf (plist-get task :process) process)
                (process-send-string process
                                     (concat (json-encode
                                              (append `((action . ,(plist-get task :action))
                                                        (cancel . ,(plist-get task :cancel)))
                                                      (plist-get (plist-get task :context) :request))) "\n"))
                (process-send-eof process))
            (error
             (when process
               (set-process-sentinel process #'ignore)
               (when (process-live-p process) (delete-process process)))
             (when (buffer-live-p output) (kill-buffer output))
             (cl-decf running)
             (setq lsp-bridge-source--tasks (delq task lsp-bridge-source--tasks))
             (delete-directory (plist-get task :directory) t)
             (funcall (plist-get task :callback) `((error . ,(error-message-string err)))))))))))

(defun lsp-bridge-source--finished (task process)
  (let ((buffer (process-buffer process)) response)
    (unwind-protect
        (progn
          (setq response
                (condition-case nil
                    (with-current-buffer buffer
                      (goto-char (point-min))
                      (let ((json-object-type 'alist) (json-array-type 'list) (json-false nil)) (json-read)))
                  (error '((error . "Source helper failed; see *lsp-bridge-source-log*")))))
          (unless (file-exists-p (plist-get task :cancel))
            (funcall (plist-get task :callback) response)))
      (when (buffer-live-p buffer) (kill-buffer buffer))
      (delete-directory (plist-get task :directory) t)
      (setq lsp-bridge-source--tasks (delq task lsp-bridge-source--tasks))
      (lsp-bridge-source--pump))))

(defun lsp-bridge-source--command (action)
  (when (file-remote-p default-directory) (user-error "Source tasks support local projects only"))
  (let ((context (list :request `((request_id . ,(number-to-string (cl-incf lsp-bridge-source--sequence)))
                                  (file . ,(or buffer-file-name (expand-file-name "_" default-directory)))
                                  (project . ,default-directory) (language . ,(lsp-bridge-source--language))
                                  (text . ,(buffer-substring-no-properties (point-min) (point-max)))
                                  (point . ,(- (point) (point-min)))
                                  (config . ,(lsp-bridge-source--config))))))
    (lsp-bridge-source--enqueue action context
                                (lambda (result)
                                  (with-current-buffer (get-buffer-create "*lsp-bridge-source-status*")
                                    (let ((inhibit-read-only t))
                                      (erase-buffer) (insert (pp-to-string result)) (special-mode)))
                                  (message "Source %s: %s (details: *lsp-bridge-source-status*, logs: *lsp-bridge-source-log*)"
                                           action (or (alist-get 'error result) (alist-get 'message (alist-get 'result result)) "ready"))))))
;;;###autoload
(defun lsp-bridge-source-install ()
  "Explicitly acquire missing sources when the provider supports it."
  (interactive) (lsp-bridge-source--command "install"))
;;;###autoload
(defun lsp-bridge-source-status ()
  "Probe the current project without downloads; show task counts and logs."
  (interactive)
  (message "%d source tasks queued/running" (length lsp-bridge-source--tasks))
  (lsp-bridge-source--command "status"))
;;;###autoload
(defun lsp-bridge-source-cancel ()
  "Cancel queued and running source tasks; published caches remain intact."
  (interactive)
  (dolist (task (copy-sequence lsp-bridge-source--tasks))
    (if (eq (plist-get task :state) 'queued)
        (progn (delete-directory (plist-get task :directory) t)
               (setq lsp-bridge-source--tasks (delq task lsp-bridge-source--tasks)))
      (with-temp-file (plist-get task :cancel) (insert "cancel"))))
  (message "Source cancellation requested"))
;;;###autoload
(defun lsp-bridge-source-open-documentation ()
  "Open documentation for the latest valid lookup, or resolve the current import."
  (interactive)
  (if lsp-bridge-source--documentation
      (lsp-bridge-source--browse lsp-bridge-source--documentation)
    (let* ((lsp-bridge-source-enable t)
           (position (lsp-bridge--position))
           (id (lsp-bridge-source--capture "documentation" position)))
      (if id (lsp-bridge-source--fallback buffer-file-name id position)
        (user-error "No local documentation provider for this buffer")))))

(provide 'lsp-bridge-source)
;;; lsp-bridge-source.el ends here
