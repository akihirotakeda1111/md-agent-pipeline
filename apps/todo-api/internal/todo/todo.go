package todo

import (
	"errors"
	"sort"
	"strings"
	"sync"
)

// Todo is the API domain object stored in memory and serialized by the HTTP layer.
type Todo struct {
	ID        int    `json:"id"`
	Title     string `json:"title"`
	Completed bool   `json:"completed"`
}

var (
	// ErrNotFound reports that a requested TODO identifier does not exist.
	ErrNotFound = errors.New("todo not found")
	// ErrInvalidTitle reports that a TODO title is empty after trimming.
	ErrInvalidTitle = errors.New("title is required")
)

// Repository stores TODOs in memory with concurrency safety.
type Repository struct {
	mu     sync.RWMutex
	nextID int
	items  map[int]Todo
}

// NewRepository constructs an empty in-memory TODO repository.
func NewRepository() *Repository {
	return &Repository{
		nextID: 1,
		items:  make(map[int]Todo),
	}
}

// Create validates and stores a new TODO with a false completed state.
func (r *Repository) Create(title string) (Todo, error) {
	title = strings.TrimSpace(title)
	if title == "" {
		return Todo{}, ErrInvalidTitle
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo := Todo{
		ID:        r.nextID,
		Title:     title,
		Completed: false,
	}
	r.nextID++
	r.items[todo.ID] = todo
	return todo, nil
}

// List returns all TODOs ordered by ascending identifier.
func (r *Repository) List() []Todo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	todos := make([]Todo, 0, len(r.items))
	for _, todo := range r.items {
		todos = append(todos, todo)
	}
	sort.Slice(todos, func(i, j int) bool {
		return todos[i].ID < todos[j].ID
	})
	return todos
}

// Get returns the TODO for id when it exists.
func (r *Repository) Get(id int) (Todo, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	todo, ok := r.items[id]
	return todo, ok
}

// Update replaces the title and completed state for an existing TODO.
func (r *Repository) Update(id int, title string, completed bool) (Todo, error) {
	title = strings.TrimSpace(title)
	if title == "" {
		return Todo{}, ErrInvalidTitle
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo, ok := r.items[id]
	if !ok {
		return Todo{}, ErrNotFound
	}

	todo.Title = title
	todo.Completed = completed
	r.items[id] = todo
	return todo, nil
}

// Delete removes a TODO when it exists.
func (r *Repository) Delete(id int) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, ok := r.items[id]; !ok {
		return false
	}
	delete(r.items, id)
	return true
}
