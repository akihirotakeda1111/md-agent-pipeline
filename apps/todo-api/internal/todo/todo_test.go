package todo

import (
	"errors"
	"reflect"
	"sync"
	"testing"
)

func TestRepositoryCreateTrimsAndDefaultsCompleted(t *testing.T) {
	repo := NewRepository()

	item, err := repo.Create("  buy milk  ")
	if err != nil {
		t.Fatalf("Create returned error: %v", err)
	}
	if item.ID != 1 {
		t.Fatalf("Create ID = %d, want 1", item.ID)
	}
	if item.Title != "buy milk" {
		t.Fatalf("Create Title = %q, want %q", item.Title, "buy milk")
	}
	if item.Completed {
		t.Fatalf("Create Completed = true, want false")
	}
}

func TestRepositoryRejectsBlankTitles(t *testing.T) {
	repo := NewRepository()

	if _, err := repo.Create("   "); !errors.Is(err, ErrInvalidTitle) {
		t.Fatalf("Create blank title error = %v, want ErrInvalidTitle", err)
	}
}

func TestRepositoryListOrdersByID(t *testing.T) {
	repo := NewRepository()

	first, err := repo.Create("first")
	if err != nil {
		t.Fatalf("Create first returned error: %v", err)
	}
	third, err := repo.Create("third")
	if err != nil {
		t.Fatalf("Create third returned error: %v", err)
	}
	second, err := repo.Create("second")
	if err != nil {
		t.Fatalf("Create second returned error: %v", err)
	}

	got := repo.List()
	want := []Todo{first, third, second}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("List() = %#v, want %#v", got, want)
	}
}

func TestRepositoryGetUpdateAndDelete(t *testing.T) {
	repo := NewRepository()

	created, err := repo.Create("buy milk")
	if err != nil {
		t.Fatalf("Create returned error: %v", err)
	}

	got, ok := repo.Get(created.ID)
	if !ok {
		t.Fatalf("Get(%d) reported missing item", created.ID)
	}
	if got != created {
		t.Fatalf("Get(%d) = %#v, want %#v", created.ID, got, created)
	}

	updated, err := repo.Update(created.ID, "buy oat milk", true)
	if err != nil {
		t.Fatalf("Update returned error: %v", err)
	}
	if updated.ID != created.ID || updated.Title != "buy oat milk" || !updated.Completed {
		t.Fatalf("Update returned %#v, want updated TODO", updated)
	}

	deleted := repo.Delete(created.ID)
	if !deleted {
		t.Fatalf("Delete(%d) = false, want true", created.ID)
	}
	if _, ok := repo.Get(created.ID); ok {
		t.Fatalf("Get(%d) after delete reported present", created.ID)
	}
	if repo.Delete(created.ID) {
		t.Fatalf("Delete(%d) on missing item = true, want false", created.ID)
	}
}

func TestRepositoryUnknownIdentifier(t *testing.T) {
	repo := NewRepository()

	if _, ok := repo.Get(99); ok {
		t.Fatalf("Get(99) reported present for missing item")
	}
	if _, err := repo.Update(99, "missing", false); !errors.Is(err, ErrNotFound) {
		t.Fatalf("Update missing item error = %v, want ErrNotFound", err)
	}
}

func TestRepositoryConcurrentCreateAssignsUniqueMonotonicIDs(t *testing.T) {
	repo := NewRepository()

	const n = 16
	var wg sync.WaitGroup
	results := make(chan Todo, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			item, err := repo.Create("todo")
			if err != nil {
				t.Errorf("Create returned error: %v", err)
				return
			}
			results <- item
		}(i)
	}

	wg.Wait()
	close(results)

	seen := make(map[int]struct{}, n)
	for item := range results {
		if item.ID < 1 || item.ID > n {
			t.Fatalf("Create returned ID %d outside expected range", item.ID)
		}
		if _, ok := seen[item.ID]; ok {
			t.Fatalf("duplicate ID %d returned from concurrent Create", item.ID)
		}
		seen[item.ID] = struct{}{}
	}

	if len(seen) != n {
		t.Fatalf("concurrent Create produced %d items, want %d", len(seen), n)
	}
}
