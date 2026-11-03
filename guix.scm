;; guix shell -f guix.scm     or     guix package -f guix.scm
(use-modules (guix packages)
             (guix gexp)
             (guix build-system pyproject)
             (guix build-system python)
             ((guix licenses) #:prefix license:)
             (gnu packages python-build))

(package
  (name "bliss")
  (version "0.1.0")
  (source (local-file "." "bliss-checkout"
                      #:recursive? #t
                      #:select? (lambda (file stat)
                                  (not (member (basename file)
                                               '(".git" "__pycache__" ".bliss" "result"))))))
  (build-system pyproject-build-system)
  (arguments (list #:tests? #f))        ;run with: python3 -m unittest discover tests
  (native-inputs (list python-setuptools))
  (home-page "https://example.invalid/bliss")
  (synopsis "Send a directory, env and Guix/Nix shell to another machine")
  (description
   "bliss moves the current directory, environment and shell closure to a
podman or docker container, a tmux or screen session on a remote Linux server,
or a Kubernetes pod or deployment, with a normal or an AI entrypoint.")
  (license license:gpl3+))
