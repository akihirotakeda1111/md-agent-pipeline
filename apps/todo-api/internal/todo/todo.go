package todo

import (
	"errors"
	"strings"
	"sync"
)

// Todo is the JSON representation shared by the HTTP layer and repository.
type Todo struct {
	ID        int    `json:"id"`
	Title     string `json:"title"`
	Completed bool   `json:"completed"`
}

// ErrInvalidTitle is returned when a title is empty after trimming whitespace.
var ErrInvalidTitle = errors.New("title is required")

// Repository stores todos in memory with concurrency safety.
type Repository struct {
	mu     sync.RWMutex
	nextID int
	items  map[int]Todo
}

// NewRepository creates an empty in-memory repository.
func NewRepository() *Repository {
	return &Repository{
		nextID: 1,
		items:  make(map[int]Todo),
	}
}

// Create validates and stores a new todo with a monotonically increasing ID.
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
	r.items[todo.ID] = todo
	r.nextID++
	return todo, nil
}

// List returns all todos ordered by ascending ID.
func (r *Repository) List() []Todo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	items := make([]Todo, 0, len(r.items))
	for id := 1; id < r.nextID; id++ {
		if todo, ok := r.items[id]; ok {
			items = append(items, todo)
		}
	}
	return items
}

// Get looks up a todo by ID.
func (r *Repository) Get(id int) (Todo, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	todo, ok := r.items[id]
	return todo, ok
}

// Update replaces the stored title and completed state for an existing todo.
func (r *Repository) Update(id int, title string, completed bool) (Todo, bool, error) {
	title = strings.TrimSpace(title)
	if title == "" {
		return Todo{}, false, ErrInvalidTitle
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo, ok := r.items[id]
	if !ok {
		return Todo{}, false, nil
	}

	todo.Title = title
	todo.Completed = completed
	r.items[id] = todo
	return todo, true, nil
}

// Delete removes a todo by ID.
func (r *Repository) Delete(id int) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, ok := r.items[id]; !ok {
		return false
	}
	delete(r.items, id)
	return true
}
