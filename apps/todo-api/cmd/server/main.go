package main

import (
	"log"
	"net/http"

	"example.com/md-agent-pipeline/todo-api/internal/todo"
)

func main() {
	server := &http.Server{
		Addr:    ":8080",
		Handler: todo.NewHandler(),
	}

	log.Printf("todo API scaffold listening on %s", server.Addr)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
