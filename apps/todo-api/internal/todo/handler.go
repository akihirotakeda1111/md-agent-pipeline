package todo

import "net/http"

// NewHandler returns the buildable baseline handler. The experiment replaces
// this placeholder with the TODO API described by the Task Spec.
func NewHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "TODO API not implemented in baseline", http.StatusNotImplemented)
	})
}
