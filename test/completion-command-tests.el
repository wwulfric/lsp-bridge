;;; completion-command-tests.el --- Completion items with commands -*- lexical-binding: t; -*-

;; Run: emacs -Q --batch -L . -L acm -L ../yasnippet -L ../posframe -L ../markdown-mode \
;;        -l test/completion-command-tests.el -f ert-run-tests-batch-and-exit
(require 'ert)
(require 'cl-lib)
(require 'lsp-bridge)

(defmacro completion-command-test-buffer (text &rest body)
  "Run BODY in a buffer holding TEXT with point at its end."
  (declare (indent 1))
  `(with-temp-buffer
     (insert ,text)
     (setq-local acm-backend-lsp-completion-position
                 (list :line 0 :character (current-column)))
     ,@body))

(defun completion-command-test-range (line from to)
  (list :start (list :line line :character from) :end (list :line line :character to)))

(ert-deftest completion-command-defers-insertion ()
  "An empty edit with a command leaves the text to the command."
  (completion-command-test-buffer "adults.firstOrN"
    (let (executed)
      (cl-letf (((symbol-value 'acm-backend-lsp-execute-command-func)
                 (lambda (server command) (setq executed (list server command)))))
        (acm-backend-lsp-candidate-expand
         (list :label "firstOrNull" :server "intellij-server"
               :command '(:command "jetbrains.kotlin.completion.apply" :arguments (3))
               ;; An InsertReplaceEdit: :insert and :replace, no :range.
               :textEdit (list :newText ""
                               :insert (completion-command-test-range 0 15 15)
                               :replace (completion-command-test-range 0 15 15)))
         8)
        (should (equal (buffer-string) "adults.firstOrN"))
        (should (equal executed '("intellij-server"
                                  (:command "jetbrains.kotlin.completion.apply"
                                   :arguments (3)))))))))

(ert-deftest completion-command-insert-replace-edit ()
  "An InsertReplaceEdit with text replaces its :replace range."
  (completion-command-test-buffer "val x = fo"
    (acm-backend-lsp-candidate-expand
     (list :label "foo" :server "s"
           :textEdit (list :newText "foo"
                           :insert (completion-command-test-range 0 8 10)
                           :replace (completion-command-test-range 0 8 10)))
     9)
    (should (equal (buffer-string) "val x = foo"))))

(ert-deftest completion-command-caret-follows-inserted-text ()
  "The caret follows what the command's edit inserts at it, imports aside."
  (with-temp-buffer
    (insert "package a\n\nval x = adults.firstOrN")
    (let ((buffer (current-buffer))
          (lsp-bridge--command-point nil))
      (cl-letf (((symbol-function 'lsp-bridge-call-file-api) #'ignore))
        (lsp-bridge-execute-command "s" '(:command "apply")))
      ;; The edit lands elsewhere first: an import above the caret.
      (goto-char (point-min))
      (forward-line 1)
      (insert "import b.C\n")
      (goto-char (point-max))
      (insert "ull()")
      (goto-char (point-min))
      (lsp-bridge--follow-command-point)
      (with-current-buffer buffer
        (should (equal (buffer-substring (line-beginning-position) (point))
                       "val x = adults.firstOrNull()")))
      (should-not lsp-bridge--command-point))))

(ert-deftest completion-command-caret-ignores-stale-command ()
  (with-temp-buffer
    (insert "abc")
    (let ((lsp-bridge--command-point (cons (copy-marker 2 t) (- (float-time) 10))))
      (goto-char (point-max))
      (lsp-bridge--follow-command-point)
      (should (= (point) (point-max)))
      (should-not lsp-bridge--command-point))))

;;; completion-command-tests.el ends here
