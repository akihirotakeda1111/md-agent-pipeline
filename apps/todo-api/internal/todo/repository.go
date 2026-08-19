package todo

import (
	"errors"
	"sort"
	"strings"
	"sync"
)

var errTitleRequired = errors.New("title is required")

// Repository stores todos in memory for the lifetime of the process.
type Repository struct {
	mu     sync.RWMutex
	nextID int
	items  map[int]Todo
}

// NewRepository returns an empty in-memory repository.
func NewRepository() *Repository {
	return &Repository{
		nextID: 1,
		items:  make(map[int]Todo),
	}
}

// Create stores a todo with a new monotonically increasing ID.
func (r *Repository) Create(title string) (Todo, error) {
	trimmed := strings.TrimSpace(title)
	if trimmed == "" {
		return Todo{}, errTitleRequired
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo := Todo{
		ID:        r.nextID,
		Title:     trimmed,
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

	ids := make([]int, 0, len(r.items))
	for id := range r.items {
		ids = append(ids, id)
	}
	sort.Ints(ids)

	items := make([]Todo, 0, len(ids))
	for _, id := range ids {
		items = append(items, r.items[id])
	}
	return items
}

// Get returns the todo for id if it exists.
func (r *Repository) Get(id int) (Todo, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()

	todo, ok := r.items[id]
	return todo, ok
}

// Update replaces the title and completed state for an existing todo.
func (r *Repository) Update(id int, title string, completed bool) (Todo, bool, error) {
	trimmed := strings.TrimSpace(title)
	if trimmed == "" {
		return Todo{}, false, errTitleRequired
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo, ok := r.items[id]
	if !ok {
		return Todo{}, false, nil
	}

	todo.Title = trimmed
	todo.Completed = completed
	r.items[id] = todo
	return todo, true, nil
}

// Delete removes the todo for id and reports whether it existed.
func (r *Repository) Delete(id int) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	if _, ok := r.items[id]; !ok {
		return false
	}
	delete(r.items, id)
	return true
}

