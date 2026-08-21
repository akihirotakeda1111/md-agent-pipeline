package todo

import (
	"errors"
	"sort"
	"strings"
	"sync"
)

// Todo is the domain object managed by the in-memory repository.
type Todo struct {
	ID        int    `json:"id"`
	Title     string `json:"title"`
	Completed bool   `json:"completed"`
}

// ErrInvalidTitle reports that a title is empty after trimming whitespace.
var ErrInvalidTitle = errors.New("title is required")

// Repository stores TODOs in memory with concurrency-safe access.
type Repository struct {
	mu     sync.RWMutex
	nextID int
	todos  map[int]Todo
}

// NewRepository constructs an empty repository.
func NewRepository() *Repository {
	return &Repository{
		nextID: 1,
		todos:  make(map[int]Todo),
	}
}

func normalizeTitle(title string) (string, error) {
	title = strings.TrimSpace(title)
	if title == "" {
		return "", ErrInvalidTitle
	}

	return title, nil
}

// Create stores a new TODO with a monotonically increasing positive ID.
func (r *Repository) Create(title string) (Todo, error) {
	normalizedTitle, err := normalizeTitle(title)
	if err != nil {
		return Todo{}, err
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	todo := Todo{
		ID:        r.nextID,
		Title:     normalizedTitle,
		Completed: false,
	}
	r.todos[todo.ID] = todo
	r.nextID++

	return todo, nil
}

// List returns all TODOs ordered by ascending ID.
func (r *Repository) List() []Todo {
	r.mu.RLock()
	defer r.mu.RUnlock()

	items := make([]Todo, 0, len(r.todos))
	for _, todo := range r.todos {
		items = append(items, todo)
	}

	sortTodosByID(items)
	return items
}

// Get looks up a TODO by ID.
func (r *Repository) Get(id int) (Todo, bool) {
	if id <= 0 {
		return Todo{}, false
	}

	r.mu.RLock()
	defer r.mu.RUnlock()

	todo, ok := r.todos[id]
	return todo, ok
}

// Update replaces the stored title and completed state for an existing TODO.
func (r *Repository) Update(id int, title string, completed bool) (Todo, bool, error) {
	if id <= 0 {
		return Todo{}, false, nil
	}

	normalizedTitle, err := normalizeTitle(title)
	if err != nil {
		return Todo{}, false, err
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	current, ok := r.todos[id]
	if !ok {
		return Todo{}, false, nil
	}

	current.Title = normalizedTitle
	current.Completed = completed
	r.todos[id] = current

	return current, true, nil
}

// Delete removes a TODO by ID.
func (r *Repository) Delete(id int) (bool, error) {
	if id <= 0 {
		return false, nil
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	if _, ok := r.todos[id]; !ok {
		return false, nil
	}

	delete(r.todos, id)
	return true, nil
}

func sortTodosByID(items []Todo) {
	sort.Slice(items, func(i, j int) bool {
		return items[i].ID < items[j].ID
	})
}
