package todo

import (
	"fmt"
	"reflect"
	"sync"
	"testing"
)

func TestRepositoryCreateTrimsTitleAndDefaultsCompleted(t *testing.T) {
	repo := NewRepository()

	item, err := repo.Create("  buy milk  ")
	if err != nil {
		t.Fatalf("Create() error = %v", err)
	}

	want := Todo{ID: 1, Title: "buy milk", Completed: false}
	if item != want {
		t.Fatalf("Create() = %#v, want %#v", item, want)
	}

	list := repo.List()
	if !reflect.DeepEqual(list, []Todo{want}) {
		t.Fatalf("List() = %#v, want %#v", list, []Todo{want})
	}
}

func TestRepositoryListOrdersByAscendingID(t *testing.T) {
	repo := NewRepository()

	repo.mu.Lock()
	repo.todos[3] = Todo{ID: 3, Title: "third", Completed: true}
	repo.todos[1] = Todo{ID: 1, Title: "first", Completed: false}
	repo.todos[2] = Todo{ID: 2, Title: "second", Completed: true}
	repo.mu.Unlock()

	list := repo.List()
	wantIDs := []int{1, 2, 3}
	if len(list) != len(wantIDs) {
		t.Fatalf("List() length = %d, want %d", len(list), len(wantIDs))
	}

	for i, todo := range list {
		if todo.ID != wantIDs[i] {
			t.Fatalf("List()[%d].ID = %d, want %d", i, todo.ID, wantIDs[i])
		}
	}
}

func TestRepositoryGetUpdateAndDelete(t *testing.T) {
	repo := NewRepository()

	created, err := repo.Create("initial")
	if err != nil {
		t.Fatalf("Create() error = %v", err)
	}

	got, ok := repo.Get(created.ID)
	if !ok {
		t.Fatalf("Get(%d) = missing, want present", created.ID)
	}
	if got != created {
		t.Fatalf("Get(%d) = %#v, want %#v", created.ID, got, created)
	}

	if _, ok := repo.Get(99); ok {
		t.Fatalf("Get(99) = present, want missing")
	}

	updated, ok, err := repo.Update(created.ID, "  updated title  ", true)
	if err != nil {
		t.Fatalf("Update() error = %v", err)
	}
	if !ok {
		t.Fatalf("Update(%d) = missing, want present", created.ID)
	}
	wantUpdated := Todo{ID: created.ID, Title: "updated title", Completed: true}
	if updated != wantUpdated {
		t.Fatalf("Update() = %#v, want %#v", updated, wantUpdated)
	}

	afterUpdate, ok := repo.Get(created.ID)
	if !ok {
		t.Fatalf("Get(%d) after update = missing, want present", created.ID)
	}
	if afterUpdate != wantUpdated {
		t.Fatalf("Get(%d) after update = %#v, want %#v", created.ID, afterUpdate, wantUpdated)
	}

	deleted, err := repo.Delete(created.ID)
	if err != nil {
		t.Fatalf("Delete() error = %v", err)
	}
	if !deleted {
		t.Fatalf("Delete(%d) = false, want true", created.ID)
	}

	if _, ok := repo.Get(created.ID); ok {
		t.Fatalf("Get(%d) after delete = present, want missing", created.ID)
	}
}

func TestRepositoryUnknownIDsAreDistinguishable(t *testing.T) {
	repo := NewRepository()

	if _, ok := repo.Get(1); ok {
		t.Fatalf("Get(1) = present, want missing")
	}

	updated, ok, err := repo.Update(1, "title", false)
	if err != nil {
		t.Fatalf("Update() error = %v", err)
	}
	if ok {
		t.Fatalf("Update(1) = %#v, true, want missing", updated)
	}

	deleted, err := repo.Delete(1)
	if err != nil {
		t.Fatalf("Delete() error = %v", err)
	}
	if deleted {
		t.Fatalf("Delete(1) = true, want false")
	}
}

func TestRepositoryRejectsBlankTitles(t *testing.T) {
	repo := NewRepository()

	if _, err := repo.Create("   "); err == nil {
		t.Fatalf("Create() error = nil, want error")
	}

	item, err := repo.Create("valid")
	if err != nil {
		t.Fatalf("Create() error = %v", err)
	}

	if _, _, err := repo.Update(item.ID, "\t\n", false); err == nil {
		t.Fatalf("Update() error = nil, want error")
	}
}

func TestRepositoryConcurrentCreatesAreSafeAndDeterministic(t *testing.T) {
	repo := NewRepository()

	const n = 32
	var wg sync.WaitGroup
	errCh := make(chan error, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()

			title := fmt.Sprintf("todo-%02d", i)
			item, err := repo.Create(title)
			if err != nil {
				errCh <- err
				return
			}
			if item.Title != title {
				errCh <- fmt.Errorf("Create(%q) returned title %q", title, item.Title)
				return
			}
			if item.Completed {
				errCh <- fmt.Errorf("Create(%q) returned completed=true", title)
			}
		}(i)
	}

	wg.Wait()
	close(errCh)

	for err := range errCh {
		if err != nil {
			t.Fatalf("concurrent create failed: %v", err)
		}
	}

	list := repo.List()
	if len(list) != n {
		t.Fatalf("List() length = %d, want %d", len(list), n)
	}

	seenIDs := make(map[int]struct{}, n)
	seenTitles := make(map[string]struct{}, n)
	for _, item := range list {
		seenIDs[item.ID] = struct{}{}
		seenTitles[item.Title] = struct{}{}
	}

	for id := 1; id <= n; id++ {
		if _, ok := seenIDs[id]; !ok {
			t.Fatalf("missing ID %d from List()", id)
		}
	}

	for i := 0; i < n; i++ {
		title := fmt.Sprintf("todo-%02d", i)
		if _, ok := seenTitles[title]; !ok {
			t.Fatalf("missing title %q from List()", title)
		}
	}
}
