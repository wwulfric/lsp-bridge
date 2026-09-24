;;; source-tests.el --- Source lifecycle tests -*- lexical-binding: t; -*-
(require 'ert)
(require 'cl-lib)
(require 'lsp-bridge)

(defmacro source-test-buffer (&rest body)
  `(let ((buffer (generate-new-buffer "source-test.hs"))
         (lsp-bridge-source-enable t))
     (unwind-protect
         (save-window-excursion
           (switch-to-buffer buffer)
           (setq-local major-mode 'haskell-mode)
           (setq-local buffer-file-name "/tmp/source-test.hs")
           (insert "import Data.Traversable (for)\n")
           (goto-char 26)
           ,@body)
       (when (buffer-live-p buffer) (kill-buffer buffer)))))

(ert-deftest source-captures-unsaved-text ()
  (source-test-buffer
   (let ((id (lsp-bridge-source--capture "jump" '(:line 0 :character 25))))
     (should id)
     (should (equal (alist-get 'text (plist-get lsp-bridge-source--context :request)) (buffer-string)))
     (should (lsp-bridge-source--valid-p lsp-bridge-source--context)))))

(ert-deftest source-disabled-and-remote ()
  (source-test-buffer
   (let ((lsp-bridge-source-enable nil)) (should-not (lsp-bridge-source--capture "jump" nil)))
   (setq-local buffer-file-name "/ssh:host:/Main.hs")
   (should-not (lsp-bridge-source--capture "jump" nil))))

(ert-deftest source-invalidates-movement-edit-close ()
  (dolist (action '(move edit close))
    (source-test-buffer
     (lsp-bridge-source--capture "jump" nil)
     (let ((context lsp-bridge-source--context) (old (point)))
       (pcase action
         ('move (forward-char 1) (lsp-bridge-source--invalidate) (goto-char old))
         ('edit (insert "x") (delete-char -1))
         ('close (kill-buffer (current-buffer))))
       (should-not (lsp-bridge-source--valid-p context))))))

(ert-deftest source-old-result-cannot-jump-or-browse ()
  (source-test-buffer
   (lsp-bridge-source--capture "jump" nil)
   (let ((context lsp-bridge-source--context))
     (lsp-bridge-source--capture "jump" nil)
     (cl-letf (((symbol-function 'lsp-bridge-define--jump) (lambda (&rest _) (ert-fail "stale jump")))
               ((symbol-function 'lsp-bridge-source--browse) (lambda (&rest _) (ert-fail "stale browse"))))
       (lsp-bridge-source--resolved context nil '((result (documentation . "file:///tmp/doc.html"))))))))

(ert-deftest source-peek-documentation-does-not-browse ()
  (source-test-buffer
   (lsp-bridge-source--capture "peek" nil)
   (cl-letf (((symbol-function 'lsp-bridge-source--browse) (lambda (&rest _) (ert-fail "peek browse"))))
     (lsp-bridge-source--resolved lsp-bridge-source--context nil '((result (documentation . "file:///tmp/doc.html")))))
   (should (equal lsp-bridge-source--documentation "file:///tmp/doc.html"))))

(ert-deftest source-falls-through-to-user ()
  (source-test-buffer
   (lsp-bridge-source--capture "jump" nil)
   (let* ((called nil) (lsp-bridge-peek-ace-list nil)
         (lsp-bridge-find-def-fallback-function (lambda (_) (setq called t))))
     (lsp-bridge-source--resolved lsp-bridge-source--context nil '((result)))
     (should called))))

(ert-deftest source-read-only-jump-and-return ()
  (let ((file (make-temp-file "source-target-" nil ".hs" "module Test where\nfor = traverse\n")))
    (unwind-protect
        (source-test-buffer
         (lsp-bridge-source--capture "jump" nil)
         (let ((origin (current-buffer)) (old (point))
               (lsp-bridge-enable-predicates '(always))
               (lsp-bridge-flash-region-delay 0))
           (lsp-bridge-source--resolved lsp-bridge-source--context nil
                                       `((result (path . ,file) (line . 1) (character . 0))))
           (set-buffer (window-buffer (selected-window)))
           (should (equal (buffer-file-name) file))
           (should buffer-read-only)
           (should-not lsp-bridge-mode)
           (should (= (line-number-at-pos) 2))
           (lsp-bridge-find-def-return)
           (should (eq (current-buffer) origin))
           (should (= (point) old))))
      (when-let* ((buffer (find-buffer-visiting file))) (kill-buffer buffer))
      (delete-file file))))

(ert-deftest source-queue-limit-merge-cancel ()
  (let ((lsp-bridge-source--tasks nil) (lsp-bridge-source-max-tasks 2)
        (lsp-bridge-python-command (or (executable-find "python3") "python"))
        (worker (make-temp-file "source-worker-" nil ".py"))
        (callbacks 0))
    (with-temp-file worker
      (insert "import json,sys,time,pathlib\nr=json.loads(sys.stdin.readline())\nwhile not pathlib.Path(r['cancel']).exists(): time.sleep(.02)\nprint(json.dumps({'ok':False,'error':'cancelled'}))\n"))
    (let ((lsp-bridge-source--worker worker))
      (unwind-protect
          (progn
            (dotimes (i 3)
              (lsp-bridge-source--enqueue "install" (list :request `((project . ,(number-to-string i))))
                                          (lambda (_) (cl-incf callbacks))))
            (lsp-bridge-source--enqueue "install" (list :request '((project . "0"))) #'ignore)
            (should (= (length lsp-bridge-source--tasks) 3))
            (should (= (cl-count 'running lsp-bridge-source--tasks :key (lambda (x) (plist-get x :state))) 2))
            ;; Editing remains available while the processes are active.
            (with-temp-buffer (insert "still responsive") (should (= (buffer-size) 16)))
            (lsp-bridge-source-cancel)
            (let ((deadline (+ (float-time) 5)))
              (while (and lsp-bridge-source--tasks (< (float-time) deadline)) (accept-process-output nil .05)))
            (should-not lsp-bridge-source--tasks)
            (should (= callbacks 0)))
        (dolist (task lsp-bridge-source--tasks)
          (when-let* ((process (plist-get task :process))) (delete-process process)))
        (delete-file worker)))))

(ert-deftest source-other-window-and-return ()
  (let ((file (make-temp-file "source-other-" nil ".hs" "module Other where\n")))
    (unwind-protect
        (source-test-buffer
         (setq-local lsp-bridge-jump-to-def-in-other-window t)
         (lsp-bridge-source--capture "jump" nil)
         (let ((window (selected-window)) (origin (current-buffer)) (lsp-bridge-flash-region-delay 0))
           (lsp-bridge-source--resolved lsp-bridge-source--context nil
                                       `((result (path . ,file) (line . 0) (character . 0))))
           (should-not (eq window (selected-window)))
           (should (eq (window-buffer window) origin))
           (set-buffer (window-buffer (selected-window)))
           (should (equal buffer-file-name file))
           (lsp-bridge-find-def-return)
           (should (eq (current-buffer) origin))))
      (when-let* ((buffer (find-buffer-visiting file))) (kill-buffer buffer))
      (delete-file file))))

(ert-deftest source-existing-buffer-retains-editability ()
  (let* ((file (make-temp-file "source-existing-" nil ".hs" "module Existing where\n"))
         (existing (find-file-noselect file)))
    (unwind-protect
        (source-test-buffer
         (lsp-bridge-source--capture "jump" nil)
         (let ((lsp-bridge-flash-region-delay 0))
           (lsp-bridge-source--resolved lsp-bridge-source--context nil
                                       `((result (path . ,file) (line . 0) (character . 0)))))
         (with-current-buffer existing (should-not buffer-read-only)))
      (kill-buffer existing)
      (delete-file file))))

(ert-deftest source-read-buffer-suppresses-global-server-hooks ()
  (let* ((file (make-temp-file "source-hooks-" nil ".el" ";; external\n"))
         (emacs-lisp-mode-hook
          (list (lambda ()
                  (when (cl-every #'funcall lsp-bridge-enable-predicates)
                    (ert-fail "started server in external source"))))))
    (unwind-protect
        (source-test-buffer
         (lsp-bridge-source--capture "jump" nil)
         (let ((lsp-bridge-enable-predicates '(always)) (lsp-bridge-flash-region-delay 0))
           (lsp-bridge-source--resolved lsp-bridge-source--context nil
                                       `((result (path . ,file) (line . 0) (character . 0))))))
      (when-let* ((buffer (find-buffer-visiting file))) (kill-buffer buffer))
      (delete-file file))))

(ert-deftest source-process-start-failure-cleans-up ()
  (let ((lsp-bridge-source--tasks nil)
        (lsp-bridge-source-python-command "/nonexistent/source-python")
        (called 0))
    (dotimes (i 3)
      (lsp-bridge-source--enqueue "status" (list :request `((request_id . ,(number-to-string i))))
                                  (lambda (response) (should (alist-get 'error response)) (cl-incf called))))
    (should-not lsp-bridge-source--tasks)
    (should (= called 3))))
