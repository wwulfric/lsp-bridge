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
(declare-function lsp-bridge-find-def "lsp-bridge")
(declare-function lsp-bridge-find-def-return "lsp-bridge")
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
(defvar lsp-bridge-source--resolver nil)
(defvar-local lsp-bridge-source--origin nil)
(defvar-local lsp-bridge-source--process-context nil)
(defun lsp-bridge-source--process-settings ()
  (or lsp-bridge-source--process-context
      (list :environment (copy-sequence process-environment)
            :exec-path (copy-sequence exec-path) :python (lsp-bridge-source--python))))
(defvar lsp-bridge-source-mode-map
  (let ((map (make-sparse-keymap)))
    (define-key map (kbd "M-.") #'lsp-bridge-find-def)
    (define-key map (kbd "M-,") #'lsp-bridge-find-def-return)
    map))
(define-minor-mode lsp-bridge-source-mode
  "Navigate verified external sources without starting a language server."
  :lighter " Source" :keymap lsp-bridge-source-mode-map)
(defvar lsp-bridge-source--sequence 0)
(defvar-local lsp-bridge-source--context nil)
(defvar-local lsp-bridge-source--documentation nil)
(defvar-local lsp-bridge-source--reading nil)

(defvar lsp-bridge-source-context-update-hook nil
  "Hook run in a managed source buffer after its origin is updated.
This also runs when an existing source buffer is reused by another project.
Use `lsp-bridge-source-mode-hook' to observe mode deactivation.")

(defun lsp-bridge-source-buffer-p (&optional buffer)
  "Return non-nil if BUFFER is a managed external source buffer.
BUFFER defaults to the current buffer."
  (with-current-buffer (or buffer (current-buffer))
    (and lsp-bridge-source-mode lsp-bridge-source--reading)))

(defun lsp-bridge-source-origin-directory (&optional buffer)
  "Return BUFFER's original project directory, or nil.
The directory may be a project subdirectory.  It survives closing the
original buffer; a shared source buffer retains the latest jump's origin."
  (with-current-buffer (or buffer (current-buffer))
    (when (lsp-bridge-source-buffer-p)
      (alist-get 'project lsp-bridge-source--origin))))
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
                    :settings (lsp-bridge-source--process-settings)
                    :other lsp-bridge-jump-to-def-in-other-window
                    :request `((request_id . ,id) (file . ,buffer-file-name)
                               (version . ,(buffer-chars-modified-tick))
                               (point . ,(1- (point))) (text . ,(buffer-substring-no-properties (point-min) (point-max)))
                               (position . ,position) (project . ,(or (alist-get 'project lsp-bridge-source--origin) default-directory))
                               (origin . ,(or lsp-bridge-source--origin (make-hash-table)))
                               (server . ,(format "%s" (bound-and-true-p acm-backend-lsp-server-names)))
                               (mode . ,mode) (language . "haskell")
                               (config . ,(or (alist-get 'config lsp-bridge-source--origin) (lsp-bridge-source--config))))))
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
    (cond ((and id lsp-bridge-source-mode)
           (lsp-bridge-source--fallback buffer-file-name id position))
          (id (lsp-bridge-call-file-api method position id))
          (t (lsp-bridge-call-file-api method position)))))

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
          (let* ((request (plist-get context :request))
                 (origin (alist-get 'origin request))
                 (project-file (if (listp origin) (alist-get 'file origin))))
            (with-current-buffer (find-buffer-visiting path)
              (when lsp-bridge-source--reading
                (setq-local lsp-bridge-source--documentation url)
                (setq-local lsp-bridge-source--process-context (plist-get context :settings))
                (setq-local lsp-bridge-source--origin
                            `((file . ,(or project-file (alist-get 'file request)))
                              (project . ,(alist-get 'project request))
                              (config . ,(alist-get 'config request))
                              (documentation . ,url)))
                (lsp-bridge-source-mode 1)
                (run-hooks 'lsp-bridge-source-context-update-hook))))
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
         (settings (or (plist-get context :settings) (lsp-bridge-source--process-settings)))
         (key (if (equal action "install")
                  (list action (alist-get 'project request) (alist-get 'config request))
                (list action (alist-get 'request_id request))))
         (existing (cl-find key lsp-bridge-source--tasks :key (lambda (task) (plist-get task :key)) :test #'equal)))
    (if existing
        (message "Source task already queued or running")
      (let* ((directory (make-temp-file "lsp-bridge-source-task-" t))
             (task (list :key key :action action :context context :callback callback
                         :directory directory :cancel (expand-file-name "cancel" directory)
                         :process nil :state 'queued
                         :environment (plist-get settings :environment) :exec-path (plist-get settings :exec-path)
                         :python (plist-get settings :python)
                         :working-directory (or (alist-get 'project request) default-directory))))
        (setq lsp-bridge-source--tasks (append lsp-bridge-source--tasks (list task)))
        (lsp-bridge-source--pump)))))

(defun lsp-bridge-source--python ()
  (or lsp-bridge-source-python-command
      (if (member (file-name-base lsp-bridge-python-command) '("uv" "pipx" "python-lsp-bridge"))
          (or (executable-find "python3") (executable-find "python")
              (error "Configure lsp-bridge-source-python-command to a local Python 3.9+"))
        lsp-bridge-python-command)))

(defun lsp-bridge-source--resolver-process ()
  (let ((key (list (lsp-bridge-source--python) lsp-bridge-source--worker default-directory exec-path process-environment)))
    (when (and (process-live-p lsp-bridge-source--resolver)
               (not (equal key (process-get lsp-bridge-source--resolver 'environment))))
      (delete-process lsp-bridge-source--resolver))
    (unless (process-live-p lsp-bridge-source--resolver)
      (let ((output (generate-new-buffer " *lsp-source-session*")))
        (condition-case err
            (setq lsp-bridge-source--resolver
                  (make-process
                   :name "lsp-bridge-source-resolver" :buffer output :connection-type 'pipe
                   :coding 'utf-8-unix :noquery t
                   :stderr (get-buffer-create "*lsp-bridge-source-log*")
                   :command (list (lsp-bridge-source--python) lsp-bridge-source--worker "--server")
                   :filter (lambda (proc chunk)
                             (with-current-buffer (process-buffer proc)
                               (goto-char (point-max)) (insert chunk)
                               (when (string-suffix-p "\n" (buffer-string))
                                 (when-let* ((task (process-get proc 'task)))
                                   (lsp-bridge-source--finished task proc)))))
                   :sentinel (lambda (proc _)
                               (when (memq (process-status proc) '(exit signal))
                                 (when-let* ((task (process-get proc 'task)))
                                   (lsp-bridge-source--finished task proc))
                                 (when (buffer-live-p (process-buffer proc))
                                   (kill-buffer (process-buffer proc)))))))
          (error (kill-buffer output) (signal (car err) (cdr err))))))
    (process-put lsp-bridge-source--resolver 'environment key)
    lsp-bridge-source--resolver))

(defun lsp-bridge-source--pump ()
  (let ((running (cl-count 'running lsp-bridge-source--tasks :key (lambda (task) (plist-get task :state)))))
    (dolist (task lsp-bridge-source--tasks)
      (when (and (< running (max 1 lsp-bridge-source-max-tasks)) (eq (plist-get task :state) 'queued)
                 (not (and (equal (plist-get task :action) "resolve")
                           (process-live-p lsp-bridge-source--resolver)
                           (process-get lsp-bridge-source--resolver 'task))))
        (cl-incf running)
        (setf (plist-get task :state) 'running)
        (let* ((process-environment (plist-get task :environment))
               (exec-path (plist-get task :exec-path))
               (lsp-bridge-source-python-command (plist-get task :python))
               (default-directory (let ((dir (plist-get task :working-directory)))
                                    (if (file-directory-p dir) (file-name-as-directory dir) default-directory)))
               (persistent (equal (plist-get task :action) "resolve"))
               (output (unless persistent (generate-new-buffer " *lsp-source-result*"))) process)
          (condition-case err
              (progn
                (setq process (if persistent (lsp-bridge-source--resolver-process)
                                (make-process
				 :name "lsp-bridge-source" :buffer output :connection-type 'pipe
				 :coding 'utf-8-unix :noquery t
				 :stderr (get-buffer-create "*lsp-bridge-source-log*")
				 :command (list (lsp-bridge-source--python) lsp-bridge-source--worker)
				 :sentinel (lambda (proc _event)
                                             (when (memq (process-status proc) '(exit signal))
                                               (lsp-bridge-source--finished task proc))))))
                (when persistent (process-put process 'task task))
                (setf (plist-get task :process) process)
                (process-send-string process
                                     (concat (json-encode
                                              (append `((action . ,(plist-get task :action))
                                                        (cancel . ,(plist-get task :cancel)))
                                                      (plist-get (plist-get task :context) :request))) "\n"))
                (unless persistent (process-send-eof process)))
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
      (if (eq process lsp-bridge-source--resolver)
          (progn (process-put process 'task nil)
                 (when (buffer-live-p buffer) (with-current-buffer buffer (erase-buffer))))
        (when (buffer-live-p buffer) (kill-buffer buffer)))
      (delete-directory (plist-get task :directory) t)
      (setq lsp-bridge-source--tasks (delq task lsp-bridge-source--tasks))
      (lsp-bridge-source--pump))))

(defun lsp-bridge-source--command (action)
  (when (file-remote-p default-directory) (user-error "Source tasks support local projects only"))
  (let ((context (list :settings (lsp-bridge-source--process-settings) :request `((request_id . ,(number-to-string (cl-incf lsp-bridge-source--sequence)))
                                  (file . ,(or (alist-get 'file lsp-bridge-source--origin) buffer-file-name (expand-file-name "_" default-directory)))
                                  (project . ,(or (alist-get 'project lsp-bridge-source--origin) default-directory)) (language . ,(lsp-bridge-source--language))
                                  (text . ,(buffer-substring-no-properties (point-min) (point-max)))
                                  (point . ,(- (point) (point-min)))
                                  (config . ,(or (alist-get 'config lsp-bridge-source--origin) (lsp-bridge-source--config)))))))
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
  (when (process-live-p lsp-bridge-source--resolver)
    (delete-process lsp-bridge-source--resolver))
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
